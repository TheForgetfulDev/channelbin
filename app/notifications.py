"""
Push notification dispatch via Apprise with per-service rate limiting.

enqueue_push() is the public entry point.  Callers add to a pending queue;
each service gets its own threading.Timer, armed for that service's effective
rate limit (its own notifications.services.<name>.rate_limit_seconds override,
or push_rate_limit_seconds if it has none), and flushes only that service's
queued messages as a single combined notification. Timers are per-service
rather than shared so one service's override cannot change when another
service (with a different or no override) flushes.

A service that accepts its configuration but then fails to deliver - a revoked webhook, an
expired token, a typo'd host - raises a standing per-service alert that clears itself on
the next successful send, so the drop is announced somewhere the user still reads instead
of only in a dvr.log WARNING that nothing promotes.
"""
import logging
import threading
from datetime import datetime

from .alerts import (NOTIFICATION_SERVICE_SEND_FAILED as ALERT_SEND_FAILED,
                     NOTIFICATION_SERVICE_URL_PLACEHOLDER as ALERT_URL_PLACEHOLDER)

log = logging.getLogger(__name__)

# Service label displayed in aggregated messages
SERVICE_LABELS = {
    'discord':        'Discord',
    'home_assistant': 'Home Assistant',
    'pushover':       'Pushover',
    'smtp2go':        'SMTP2Go (Email)',
    'whatsapp':       'WhatsApp',
}

# URL format hints shown in the settings UI
SERVICE_URL_HINTS = {
    'discord':        'discord://WebhookID/WebhookToken/',
    'home_assistant': 'hassio://AccessToken@hostname/',
    'pushover':       'pover://UserKey@AppToken/',
    'smtp2go':        'mailtos://user:apikey@mail.smtp2go.com/recipient@example.com',
    'whatsapp':       'twilio://AccountSID:AuthToken@/+1FromNumber/+1ToNumber',
}

_lock = threading.Lock()
_pending: dict[str, list[dict]] = {}          # service_name → [{title, body, alert_type}]
_timers: dict[str, threading.Timer] = {}      # service_name → its own flush timer


def effective_rate_limit(svc_cfg: dict, global_rate_limit: int) -> int:
    """A service's own rate_limit_seconds override, or the global value if unset.

    Unset (key absent, or None) means "use the global limit". 0 means "unlimited" -
    flush immediately, no batching wait - which must stay distinct from unset.
    """
    override = svc_cfg.get('rate_limit_seconds')
    if override is None:
        return global_rate_limit
    return max(0, int(override))


def enqueue_push(services: list[str], title: str, body: str, alert_type: str = '', link: str = None):
    """Add a notification to the pending queue for each named service.

    Each service gets its own rate-limit timer, armed for that service's effective
    limit (effective_rate_limit), and flushes only that service's queued messages
    into a single combined send.
    services: list of service keys (e.g. ['discord', 'pushover'])
    link: optional fully-qualified URL to append to the message (e.g. a recording's
    detail page). Kept separate from `body` so it doesn't get cut off by the
    per-message truncation used when batching several messages into one send.
    """
    from .config import load_config
    cfg = load_config()
    notif_cfg = cfg.get('notifications', {})
    global_rate_limit = int(notif_cfg.get('push_rate_limit_seconds', 60))
    svc_cfg = notif_cfg.get('services', {})

    # The flush runs on a threading.Timer thread, which has no app context of its own, so
    # the app has to be captured here and carried to it - _flush needs one to record a
    # send failure. None is legitimate: enqueue_push is reachable from a bare thread with
    # no context at all, and _flush degrades to logging when it has no app to work in.
    from flask import current_app, has_app_context
    app = current_app._get_current_object() if has_app_context() else None

    broken = []
    with _lock:
        for svc in services:
            scfg = svc_cfg.get(svc, {})
            if not scfg.get('enabled'):
                continue
            url = scfg.get('url', '').strip()
            if not url:
                continue
            if url == SERVICE_URL_HINTS.get(svc):
                # Stored URL is still the field's own example placeholder, never a real
                # credential (dev/docs/BUGS.md 2026-08-11 09:00 PM) - alert instead of
                # silently dropping the message. Deferred until the lock is released,
                # since alert_placeholder_url does DB I/O and can recurse back into this
                # function via create_alert()'s own push routing.
                broken.append(svc)
                continue
            _pending.setdefault(svc, []).append({
                'title': title,
                'body': body,
                'alert_type': alert_type,
                'link': link,
                'queued_at': datetime.utcnow().isoformat(),
            })
            _arm_timer(svc, effective_rate_limit(scfg, global_rate_limit), app)

    for svc in broken:
        try:
            alert_placeholder_url(svc)
        except Exception:
            log.exception('Failed to raise placeholder-URL alert for %s', svc)


def service_label(svc: str) -> str:
    """Display name for a service key, falling back to the key itself.

    SERVICE_LABELS covers only the services the settings UI offers a card for. Apprise
    accepts any scheme it supports, and config.yaml can name one directly, so nothing on
    the alert path may assume svc is one of the labelled few - an unknown key is a real
    service that is failing, and it has to be nameable in the alert that says so.
    """
    return SERVICE_LABELS.get(svc, svc)


def _has_open_alert(alert_type: str, svc: str) -> bool:
    """True if an undismissed alert of this type already stands for svc.

    Thin name-local alias: this module keys its standing alerts on the service name, and
    reading `svc` at the six call sites is worth more than inlining the shared helper's
    generic `source`."""
    from .alerts import has_open_alert
    return has_open_alert(alert_type, svc)


def _dismiss_service_alert(alert_type: str, svc: str):
    """Clear the standing alert of this type for svc, if one is open. Alias, same as
    _has_open_alert above."""
    from .alerts import dismiss_open_alerts
    dismiss_open_alerts(alert_type, svc)


def alert_placeholder_url(svc: str):
    """Raise the standing alert for svc's URL being its own example placeholder, unless
    one is already open. Create-or-dismiss, keyed by source=svc, mirrors
    app/health_score.py::_apply_failing_alert - but only the create half lives here; the
    dismiss half runs at the point the URL is actually fixed (see
    dismiss_placeholder_url_alert, called from the settings save/remove routes) rather
    than on this hot push path.
    """
    if _has_open_alert(ALERT_URL_PLACEHOLDER, svc):
        return
    from .alerts import create_alert
    label = service_label(svc)
    create_alert(
        ALERT_URL_PLACEHOLDER,
        f'{label} push is not configured',
        body=(f"The stored URL for {label} is still the example placeholder text shown in "
              f"its own field, not a real credential, so its pushes are being dropped. "
              f"Paste the service's real Apprise URL in Settings > Notifications to fix it."),
        source=svc)


def dismiss_placeholder_url_alert(svc: str):
    """Clear the standing NOTIFICATION_SERVICE_URL_PLACEHOLDER alert for svc, if one is
    open. Called from the settings save/remove routes once svc's stored URL is no longer
    its own placeholder - see alert_placeholder_url for the create half."""
    _dismiss_service_alert(ALERT_URL_PLACEHOLDER, svc)


def alert_send_failed(svc: str, reason: str, dropped: int):
    """Raise the standing alert for svc's sends failing, unless one is already open.

    `reason` must be one of this module's own fixed phrasings, never text from Apprise or
    from an exception: the alert body is rendered in the UI and can be pushed off-box, and
    the maskers in app/url_utils.py are anchored to http(s) URLs, so a `pover://` or
    `discord://` URL embedded in third-party error text would survive them intact.

    Dedupes against an open alert rather than refreshing it, matching alert_placeholder_url
    - the first failure's reason is the one that stands until the alert clears. That check
    is also what bounds the recursion when a user routes this very alert type back to the
    failing service: the doomed push it enqueues fails, finds the alert already open, and
    returns without creating or enqueuing anything further.
    """
    if _has_open_alert(ALERT_SEND_FAILED, svc):
        return
    from .alerts import create_alert
    label = service_label(svc)
    create_alert(
        ALERT_SEND_FAILED,
        f'{label} push notifications are failing',
        body=(f"ChannelBin could not deliver to {label} because {reason}, and "
              f"{dropped} queued notification(s) were dropped. Anything else routed to "
              f"{label} will keep being dropped until it works again. Check the service's "
              f"URL in Settings > Notifications; this alert clears itself on the next "
              f"successful send."),
        source=svc)


def dismiss_send_failure_alert(svc: str):
    """Clear the standing NOTIFICATION_SERVICE_SEND_FAILED alert for svc, if one is open.

    Called on a successful send (the flush path and the settings Test button) and from the
    settings save/remove routes - the latter because a service whose URL was cleared or
    disabled has no successful send left to clear its own alert, and would otherwise stand
    accused forever.
    """
    _dismiss_service_alert(ALERT_SEND_FAILED, svc)


def _arm_timer(svc: str, delay: int, app=None):
    """Start svc's flush timer if not already running. Must be called under _lock.

    `app` is the Flask app the flush records its outcome against; the first enqueue in a
    window supplies it, since a rearm is skipped while a timer stands.
    """
    if svc in _timers:
        return
    timer = threading.Timer(delay, _flush, args=(svc, app))
    timer.daemon = True
    _timers[svc] = timer
    timer.start()


def _flush(svc: str, app=None):
    """Send svc's pending notifications as one Apprise call and record the outcome.

    Runs on a threading.Timer thread, so `app` (captured by enqueue_push) is the only
    route to an app context here. Without one the outcome is logged and nothing is
    recorded - a push path that never had a context cannot reach the database anyway.
    """
    with _lock:
        messages = _pending.pop(svc, [])
        _timers.pop(svc, None)

    if not messages:
        return
    try:
        ok, reason = _send_service(svc, messages)
    except Exception as exc:
        log.exception('Failed to send push notification to %s', svc)
        # Class name only: an exception's message can carry the service URL, and this
        # string reaches an alert body. See alert_send_failed.
        ok, reason = False, f'the send raised {type(exc).__name__}'

    if app is None:
        if not ok:
            log.warning('Push to %s failed (%s) with no app context to record it against; '
                        '%d message(s) dropped', svc, reason, len(messages))
        return

    try:
        with app.app_context():
            if ok:
                dismiss_send_failure_alert(svc)
            else:
                alert_send_failed(svc, reason, len(messages))
    except Exception:
        log.exception('Failed to record push outcome for %s', svc)


def _send_service(svc: str, messages: list[dict]) -> tuple[bool, str]:
    """Build and fire one Apprise notification for a single service.

    Returns (ok, reason). `reason` is one of this module's own fixed phrasings so it is
    safe to render and to push - never Apprise's own error text, which can quote the
    credentialed URL. Empty on success.
    """
    try:
        import apprise
    except ImportError:
        log.error('apprise package not installed; cannot send push notifications')
        return False, 'the apprise package is not installed'

    from .config import load_config
    cfg = load_config()
    url = cfg.get('notifications', {}).get('services', {}).get(svc, {}).get('url', '').strip()
    if not url:
        # Reachable only when the URL is cleared between enqueue and flush - enqueue_push
        # skips a service with no URL. Still a real drop, so it is still a failure; the
        # settings routes dismiss the alert when the clearing was deliberate.
        log.warning('No URL configured for %s at flush time; %d message(s) dropped',
                    svc, len(messages))
        return False, 'no URL is configured for it'

    if len(messages) == 1:
        title = messages[0]['title']
        body = messages[0]['body'] or title
        if messages[0].get('link'):
            body = f"{body}\n{messages[0]['link']}"
    else:
        title = f'ChannelBin: {len(messages)} alerts'
        body_lines = []
        for m in messages:
            body_lines.append(f"• [{m['alert_type']}] {m['title']}")
            if m.get('body') and m['body'] != m['title']:
                body_lines.append(f"  {m['body'][:200]}")
            if m.get('link'):
                body_lines.append(f"  {m['link']}")
        body = '\n'.join(body_lines)

    ap = apprise.Apprise()
    ap.add(url)
    if not ap.notify(title=title, body=body):
        log.warning('Apprise notify returned False for service %s; %d message(s) dropped',
                    svc, len(messages))
        return False, 'the service rejected the notification (check its URL and credentials)'
    log.debug('Push sent to %s (%d message(s))', svc, len(messages))
    return True, ''


def send_test(svc: str) -> tuple[bool, str]:
    """Send a single test notification to a service immediately (no rate limit).

    Returns (success, error_message).
    """
    try:
        import apprise
    except ImportError:
        return False, 'apprise package not installed'

    from .config import load_config
    cfg = load_config()
    url = cfg.get('notifications', {}).get('services', {}).get(svc, {}).get('url', '').strip()
    if not url:
        return False, 'No URL configured for this service'

    ap = apprise.Apprise()
    ap.add(url)
    ok = ap.notify(
        title='ChannelBin test notification',
        body='This is a test notification from ChannelBin. Your push notification service is working correctly.',
    )
    if ok:
        # A successful test is a successful send, so it clears the standing failure alert
        # the same way a successful flush does - the user fixing the URL and pressing Test
        # is the most direct way this ever gets resolved.
        dismiss_send_failure_alert(svc)
        return True, ''
    return False, 'Apprise returned failure - check the URL format'
