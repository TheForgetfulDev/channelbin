import json
import logging
import os
import re
import signal
import subprocess
import threading
import uuid
import yaml
from io import BytesIO

from ruamel.yaml.error import YAMLError as RoundTripYAMLError
from flask import (Blueprint, render_template, request, redirect, url_for, flash,
                   jsonify, send_file, current_app, session)

from .. import health_bands
from ..config import (load_config, save_config, config_default, is_restart_needed,
                      RESTART_REQUIRED_KEYS, set_nested, mask_config, redact_sensitive_diff_lines, _is_sensitive_path,
                      _load_config_file, config_write_lock, load_for_edit, changed_from_default,
                      check_config_text)
from ..database import (
    REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING,
    REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING,
)
from ..accounts import (TEMPLATE_VARIABLES, _TAG_TOKEN_RE, render_filename_template,
                        tag_template_variables)
from ..recorder import _safe_name, FINISHED_IMAGE_CHOICES
from ..postprocessor import VIDEO_ENCODERS
from ..channel_search import PAGE_SIZE_OPTIONS, configured_page_size
from ..alerts import ALERT_TYPES, RETIRED_ALERT_TYPES
from ..notifications import (SERVICE_LABELS, SERVICE_URL_HINTS, dismiss_placeholder_url_alert,
                             dismiss_send_failure_alert)
from ..tz_utils import get_display_tz, format_local, to_local, to_naive_utc, parse_hhmm

settings_bp = Blueprint('settings', __name__)
log = logging.getLogger(__name__)

# The /api/user-prefs key holding the Settings page's Basic/Advanced choice: true shows
# Advanced, anything else (including never chosen) shows Basic. Named once here - the
# route stamps the first paint from it and hands it to settings.js to write back, so the
# two cannot disagree on the spelling (DESIGN.md 15.9).
SETTINGS_VIEW_PREF = 'settings_show_advanced'


def _page_size_choice(value):
    """`search.page_size` as the int it is stored as, or None when it is not on the menu. The
    select posts a string, and the raw YAML editor may hold either spelling."""
    if isinstance(value, bool):
        return None
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size in PAGE_SIZE_OPTIONS else None


def _raw_yaml_problems(text):
    """`check_config_text()` plus the rules the field routes enforce, which the raw editor
    would otherwise walk around (enforcement lives server-side). The one answer both its
    Validate button and its Save give (dev/changelog/1044)."""
    data, problems = check_config_text(text)
    if data is None:
        return data, problems

    def refuse(path, message):
        problems.append({'severity': 'error', 'path': path, 'line': None, 'message': message})

    # The Security card disables the enable toggle client-side until a password exists. A
    # masked '********' still counts as "a password is set" - restore_masked_secrets()
    # resolves it to the stored hash inside save_config().
    auth_block = data.get('auth') if isinstance(data.get('auth'), dict) else {}
    if auth_block.get('enabled') and not str(auth_block.get('password_hash') or '').strip():
        refuse('auth.enabled', 'Cannot turn on the login gate with no password set - set one '
                               'from the Security card first.')
    testing = data.get('channel_testing') if isinstance(data.get('channel_testing'), dict) else {}
    window_block = testing.get('window') if isinstance(testing.get('window'), dict) else {}
    win_start, win_end = window_block.get('start'), window_block.get('end')
    try:
        for value in (win_start, win_end):
            if value is not None:
                parse_hhmm(value)
    except (TypeError, ValueError, AttributeError):
        refuse('channel_testing.window', 'start and end must be times like "01:30".')
    else:
        if win_start is not None and win_start == win_end:
            refuse('channel_testing.window', 'start and end cannot be the same time - a '
                                             'zero-length window would never run anything.')
    # dev/changelog/771: the same band validation as api_settings_field.
    bands_block = testing.get('health_bands')
    if isinstance(bands_block, dict):
        floors = dict(health_bands.DEFAULT_FLOORS)
        floors.update({k: v for k, v in bands_block.items() if k in floors})
        problem = health_bands.validate_floors(floors)
        if problem:
            refuse('channel_testing.health_bands', f'{problem}.')
    failing = testing.get('failing_band')
    if failing is not None and failing not in health_bands.FAILING_VALUES:
        refuse('channel_testing.failing_band',
               'Must be one of ' + ', '.join(health_bands.FAILING_VALUES) + '.')
    search = data.get('search') if isinstance(data.get('search'), dict) else {}
    page_size = search.get('page_size')
    if page_size is not None and _page_size_choice(page_size) is None:
        refuse('search.page_size',
               'Must be one of ' + ', '.join(map(str, PAGE_SIZE_OPTIONS)) + '.')
    return data, problems


@settings_bp.route('/api/settings/validate', methods=['POST'])
def api_settings_validate():
    """Check the raw editor's text without saving it. Body: {text}."""
    text = (request.get_json(silent=True) or {}).get('text')
    if not isinstance(text, str):
        return jsonify({'error': 'text is required'}), 400
    _, problems = _raw_yaml_problems(text)
    return jsonify({'success': True, 'problems': problems,
                    'valid': not any(p['severity'] == 'error' for p in problems)})


def _save_raw_yaml(data):
    """save_config() and the side effects a changed key needs; returns the flash text."""
    changed = save_config(data)
    if any(path.startswith('auth.') for path, _, _ in changed):
        from ..auth import refresh_auth
        refresh_auth(current_app._get_current_object())
    from ..toolchain import TOOLCHAIN_CONFIG_KEYS
    if any(path in TOOLCHAIN_CONFIG_KEYS for path, _, _ in changed):
        from ..toolchain import report_tool_state
        report_tool_state(source='settings')
    if any(path == 'search.tag_id_cache_enabled' for path, _, _ in changed):
        from ..channel_search import clear_tag_channel_ids_cache
        clear_tag_channel_ids_cache()
    if any(path == 'search.standing_breakdown_cache_ttl_seconds' for path, _, _ in changed):
        from ..channel_search import clear_standing_breakdown_cache
        clear_standing_breakdown_cache()
    if any(path == 'recording.logo_cache.enabled' for path, _, _ in changed):
        from ..scheduler import apply_logo_cache_schedule
        apply_logo_cache_schedule()
    if not changed:
        return 'No changes detected.'
    gpu = _refresh_gpu_after_save(changed)
    gpu_note = f' {_gpu_trial_sentence(gpu)}' if gpu else ''
    if any(path in RESTART_REQUIRED_KEYS for path, _, _ in changed):
        return ('Settings saved. Restart the service for all changes to take effect.'
                + gpu_note)
    return 'Settings saved.' + gpu_note


def _refresh_gpu_after_save(changed):
    """Re-answer the GPU Readiness lines when a save moved a key they read, and return the
    gpu_encoder Result while the GPU encoder is on (None otherwise, or when nothing it reads
    changed). Runs the trial inline, after save_config() has returned and outside the config
    lock, so the page can say whether the GPU works in the same response - one second
    normally, bounded by the trial's own deadline (dev/changelog/1127)."""
    from ..readiness import GPU_CONFIG_KEYS, refresh_gpu_checks
    if not any(p in GPU_CONFIG_KEYS for p, _, _ in changed):
        return None
    return refresh_gpu_checks(load_config())


def _gpu_trial_sentence(result):
    from ..readiness import READY
    if result.status == READY:
        return f'GPU encoder test passed: {result.found}.'
    return f'GPU encoder test failed, so conversions will use the CPU: {result.found}'


@settings_bp.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'GET':
        return _render_settings()
    raw = request.form.get('config_yaml', '')
    data, problems = _raw_yaml_problems(raw)
    if any(p['severity'] == 'error' for p in problems):
        # Rendered, not redirected: a redirect reloads the editor from disk and throws away
        # everything the user typed.
        flash('config.yaml was not saved - see the problems under the editor.', 'error')
        return _render_settings(yaml_text=raw, yaml_problems=problems), 400
    try:
        message = _save_raw_yaml(data)
    except (OSError, yaml.YAMLError, RoundTripYAMLError) as exc:
        log.error('Settings: saving config.yaml from the raw editor failed: %s', exc)
        flash(f'config.yaml could not be written: {exc}', 'error')
        return _render_settings(yaml_text=raw), 500
    warnings = sum(1 for p in problems if p['severity'] == 'warning')
    if warnings:
        message += f' {warnings} warning{"s" if warnings != 1 else ""} - press Validate to see them.'
    flash(message, 'success')
    return redirect(url_for('settings.settings'))


def _render_settings(yaml_text=None, yaml_problems=None):
    """The Settings page. `yaml_text` puts the raw editor back on text that was not saved,
    with the config.yaml tab open and `yaml_problems` listed under it."""
    # Mask sensitive leaves on both render paths - the individual field widgets and the raw
    # YAML editor dump. The masked value round-trips back to the stored secret on save.
    unmasked_cfg = load_config()
    cfg = mask_config(unmasked_cfg)
    cfg_yaml = (yaml_text if yaml_text is not None
                else yaml.dump(cfg, default_flow_style=False, sort_keys=False))
    # Coerced rather than read raw: a config written before _m014 still holds a boolean
    # here, and the select must land on the equivalent mode instead of no option at all.
    from ..accounts import NORM_DISABLED, NORM_MODES, coerce_normalization_mode
    from zoneinfo import available_timezones
    timezone_options = [(tz, tz) for tz in sorted(available_timezones())]

    # "Is my maintenance window big enough?" - the heaviest day's booked total against the
    # window length. Window bounds/duration are not secret, so the unmasked cfg is fine here.
    from ..check_window import window_plan
    from ..fmt_utils import fmt_duration_hm, fmt_duration_phrase
    from .channel_tests import _RECUR_DAY_LABELS
    plan = window_plan(unmasked_cfg.get('channel_testing', {}))
    booked_days = [(d, plan['days'][d]) for d in range(1, 8) if plan['days'][d]['checks']]
    window_capacity_line = None
    if booked_days:
        heaviest_day, heaviest = max(booked_days, key=lambda kv: kv[1]['total_seconds'])
        window_capacity_line = (
            f"Booked: heaviest day is {_RECUR_DAY_LABELS[heaviest_day]} at "
            f"{fmt_duration_phrase(heaviest['total_seconds'])} of {fmt_duration_hm(plan['window_seconds'])}."
        )
        if heaviest['total_seconds'] > plan['window_seconds']:
            over_count = len(heaviest['checks'])
            window_capacity_line += (
                f" {over_count} check{'s' if over_count != 1 else ''} may not finish."
            )

    # Server-rendered so the page paints in the saved view instead of flashing the other
    # one first. One row, one query - it does not grow with anything.
    from ..database import db, UserPref
    view_pref = db.session.get(UserPref, SETTINGS_VIEW_PREF)
    show_advanced = bool(view_pref and view_pref.value and json.loads(view_pref.value) is True)

    return render_template(
        'settings.html', cfg=cfg, cfg_yaml=cfg_yaml,
        yaml_open=yaml_text is not None, yaml_problems=yaml_problems or [],
        show_advanced=show_advanced, view_pref_key=SETTINGS_VIEW_PREF,
        # Paths only - the unmasked config is compared, but no value leaves this line.
        changed_paths=set(changed_from_default(unmasked_cfg)),
        restart_needed=is_restart_needed(),
        norm_mode_options=[(value, label) for value, label, _ex in NORM_MODES],
        norm_mode_value=(coerce_normalization_mode(cfg.get('sync', {}).get('url_normalization'))
                         or NORM_DISABLED),
        window_capacity_line=window_capacity_line,
        page_size_options=[(n, str(n)) for n in PAGE_SIZE_OPTIONS],
        page_size_value=configured_page_size(unmasked_cfg),
        timezone_options=timezone_options,
    )


# ---------------------------------------------------------------------------
# GUI settings API
# ---------------------------------------------------------------------------

@settings_bp.route('/api/settings')
def api_settings_get():
    """Return current merged config as JSON, with sensitive leaves masked."""
    return jsonify(mask_config(load_config()))


@settings_bp.route('/api/settings/field', methods=['POST'])
def api_settings_field():
    """Save a single config field. Body: {path, value}."""
    data = request.get_json(silent=True) or {}
    path = data.get('path', '')
    value = data.get('value')

    if not path:
        return jsonify({'error': 'Missing path'}), 400

    # These new numeric settings drive supervised-conversion loops/timeouts; a negative or
    # absurd value would misbehave. Coerce to a non-negative int, and cap the poll interval
    # so the UI stays live. (General settings validation is otherwise pre-existing-absent.)
    _numeric_clamps = {
        'recording.post_process.max_restart_attempts': (0, None),
        # Floor of 1, not 0: this one is never a disable switch. A 0 would kill every
        # conversion on its first poll, before ffmpeg could possibly have muxed anything.
        'recording.post_process.pre_output_timeout_seconds': (1, None),
        'recording.post_process.stall_seconds': (0, None),
        # Same split as the conversion pair above: the pre-output budget is never a disable
        # switch (a 0 would kill every join on its first poll), the stall budget is.
        'ffmpeg.concat_pre_output_timeout_seconds': (1, None),
        'ffmpeg.concat_stall_seconds': (0, None),
        # 0 disables the read timeout entirely; a negative one would be emitted as a
        # negative microsecond count, which ffmpeg reads as "no timeout" on some builds
        # and rejects on others.
        'ffmpeg.read_timeout_seconds': (0, None),
        'recording.post_process.progress_interval_seconds': (1, 60),
        # 0 is a real answer here - the program's own first moment - so no floor above it.
        'recording.live_thumbnail.poster_frame_offset_seconds': (0, None),
        'recording.post_process.video_crf': (0, 51),
        'recording.post_process.vaapi_qp': (0, 51),
        'recording.post_process.audio_bitrate_kbps': (32, 320),
        'channel_testing.window.dispatch_interval_minutes': (1, None),
    }
    if path in _numeric_clamps:
        lo, hi = _numeric_clamps[path]
        try:
            value = int(value)
        except (TypeError, ValueError):
            return jsonify({'error': 'Value must be a whole number'}), 400
        value = max(lo, value)
        if hi is not None:
            value = min(hi, value)

    # Same shape as _numeric_clamps above, for the one setting that is legitimately
    # fractional (recording.post_process.collision_lookahead_multiplier - "2" means 2x
    # realtime, "0.5" means half).
    _float_clamps = {
        'recording.post_process.collision_lookahead_multiplier': (0.1, None),
    }
    if path in _float_clamps:
        lo, hi = _float_clamps[path]
        try:
            value = float(value)
        except (TypeError, ValueError):
            return jsonify({'error': 'Value must be a number'}), 400
        value = max(lo, value)
        if hi is not None:
            value = min(hi, value)

    # load → validate → mutate → save is ONE unit under config.yaml's write lock, the same
    # whole-read-modify-write rule retry_on_locked applies to DB commits. save_config()
    # writes whatever it is handed, so two of these routes in flight (settings.js saves per
    # field, against a threaded server) would otherwise each merge onto the same stale
    # snapshot and the second write would silently drop the first field
    # (dev/docs/BUGS.md 2026-08-15 @ 05:32:07 PM ET).
    #
    # Two dicts, two jobs (load_for_edit): validate against `cfg`, the effective config,
    # because a value the user has never set exists only as a default - but write the leaf
    # into `file_cfg`, the raw config.yaml, so the save persists what was chosen rather than
    # freezing today's defaults into the file (dev/changelog/727).
    with config_write_lock:
        cfg, file_cfg = load_for_edit()

        # end < start is valid (the window crosses midnight, per app/check_window.py) - only a
        # zero-length window (start == end) is rejected, since it would never run anything.
        if path in ('channel_testing.window.start', 'channel_testing.window.end'):
            try:
                parse_hhmm(value)
            except ValueError:
                return jsonify({'error': 'Time must be in HH:MM format'}), 400
            window_cfg = cfg.get('channel_testing', {}).get('window', {})
            other_key = 'end' if path.endswith('.start') else 'start'
            if window_cfg.get(other_key) == value:
                return jsonify({'error': 'Start and end time cannot be the same - a '
                                         'zero-length window would never run anything'}), 400

        # The three band cut points are one setting saved as three fields, so each save has
        # to be judged against the OTHER two as they currently stand - a lone "good: 95"
        # that crosses the Great floor is what makes the scale unreadable, and the value
        # itself is fine in isolation. Rejected rather than repaired: a silently-corrected
        # cut point leaves the settings page showing one number and every badge using
        # another (dev/changelog/771).
        if path.startswith('channel_testing.health_bands.'):
            key = path.rsplit('.', 1)[1]
            try:
                value = int(value)
            except (TypeError, ValueError):
                return jsonify({'error': 'Value must be a whole number'}), 400
            floors = dict(health_bands.DEFAULT_FLOORS)
            floors.update({k: v for k, v in
                           (cfg.get('channel_testing', {}).get('health_bands') or {}).items()
                           if k in floors and isinstance(v, int)})
            floors[key] = value
            problem = health_bands.validate_floors(floors)
            if problem:
                return jsonify({'error': f'Health band cut points: {problem}'}), 400

        if path == 'channel_testing.failing_band':
            if value not in health_bands.FAILING_VALUES:
                return jsonify({'error': 'Unknown band - pick one of '
                                         + ', '.join(health_bands.FAILING_VALUES)}), 400

        # Enforcement lives server-side: the page offers a two-option select, and a third
        # value stored here would reach a branch chain that names both real states and
        # would silently render as one of them.
        if path == 'recording.live_thumbnail.finished_image':
            if value not in FINISHED_IMAGE_CHOICES:
                return jsonify({'error': 'Pick one of '
                                         + ', '.join(FINISHED_IMAGE_CHOICES)}), 400

        # Same rule as the finished-image choice above: the postprocessor branches on this
        # value by name, and an unknown one would be re-encoded in software with a warning
        # rather than doing what the person thought they chose.
        if path == 'recording.post_process.video_encoder':
            if value not in VIDEO_ENCODERS:
                return jsonify({'error': 'Pick one of ' + ', '.join(VIDEO_ENCODERS)}), 400

        if path == 'search.page_size':
            value = _page_size_choice(value)
            if value is None:
                return jsonify({'error': 'Rows per page must be one of '
                                         + ', '.join(map(str, PAGE_SIZE_OPTIONS))}), 400

        # Enforcement lives server-side (CLAUDE.md): the Integrations card disables this
        # toggle client-side until a key exists, but the route must refuse it too. Unlike
        # auth.enabled below, disabling does NOT clear the stored key hash - the Home
        # Assistant integration keeps the plaintext key in its own config, and re-enabling
        # later should keep working with it rather than forcing a fresh key every time.
        if path == 'integrations.home_assistant.enabled' and value:
            existing_key_hash = (cfg.get('integrations', {}).get('home_assistant', {})
                                 .get('api_key_hash') or '').strip()
            if not existing_key_hash:
                return jsonify({'error': 'Generate an API key before enabling the Home '
                                         'Assistant integration'}), 400

        # Enforcement lives server-side (CLAUDE.md): the Security card disables this toggle
        # client-side until a password exists, but the route must refuse it too.
        if path == 'auth.enabled':
            existing_hash = (cfg.get('auth', {}).get('password_hash') or '').strip()
            if value:
                if not existing_hash:
                    return jsonify({'error': 'Set a password before enabling the login gate'}), 400
            elif existing_hash:
                # Disabling clears the stored password too, so re-enabling later always starts
                # from a fresh password instead of trapping the user behind a "current password"
                # they picked once for testing and never wrote down (2026-08-06).
                from ..auth import gate_active
                if gate_active(cfg.get('auth') or {}):
                    from werkzeug.security import check_password_hash
                    current_password = data.get('current_password') or ''
                    if not current_password or not check_password_hash(existing_hash, current_password):
                        return jsonify({'error': 'Current password is incorrect'}), 400
                set_nested(file_cfg, 'auth.password_hash', '')

        set_nested(file_cfg, path, value)
        changed = save_config(file_cfg)
        # Read back under the same lock, so the answer describes this save and not one that
        # landed after it. The page marks a changed row from it, so setting a field back to
        # its default clears the mark without a reload.
        still_changed = path in changed_from_default(load_config())

    needs_restart = any(p in RESTART_REQUIRED_KEYS for p, _, _ in changed)

    if any(p.startswith('auth.') for p, _, _ in changed):
        from ..auth import refresh_auth
        refresh_auth(current_app._get_current_object())

    # Both binary paths are read at every spawn rather than at startup, so a change takes
    # effect live - which means the answer to "which ffmpeg is this" has to be re-probed
    # live too, and a missing-tool alert has to be raised or cleared without waiting for a
    # restart. Asked by the shared key list, so a third key cannot reach one of these two
    # hooks and not the other.
    from ..toolchain import TOOLCHAIN_CONFIG_KEYS
    if any(p in TOOLCHAIN_CONFIG_KEYS for p, _, _ in changed):
        from ..toolchain import report_tool_state
        report_tool_state(source='settings')

    # Either of the two numbers can make the read timeout inert, so both are watched - a
    # stall timeout lowered past a perfectly good read timeout is the same misconfiguration
    # as a read timeout raised past the stall timeout, and only one of them is on the ffmpeg
    # card. Re-read rather than reusing `cfg`, which was snapshotted before this save.
    if any(p in ('ffmpeg.read_timeout_seconds', 'watchdog.stall_timeout_seconds')
           for p, _, _ in changed):
        from ..proc_utils import report_read_timeout_state
        report_read_timeout_state(load_config(), source='settings')

    # Changing the global sync interval immediately reschedules all Default accounts
    if path == 'sync.sync_interval_hours':
        from ..scheduler import schedule_all_account_syncs
        schedule_all_account_syncs(current_app._get_current_object(),
                                   force_reschedule_defaults=True)

    # start/end are re-read live by the dispatcher and window_close, so this is purely
    # about keeping hc_window_close's CronTrigger and every window check's displayed
    # next-run in sync with a changed end time - not a prerequisite for the new bounds
    # to take effect.
    if path in ('channel_testing.window.start', 'channel_testing.window.end'):
        from ..scheduler import reschedule_window_jobs
        reschedule_window_jobs()

    # Turning the cache off must actually free the RAM, not just stop it growing - and
    # turning it back on should start from a clean slate rather than an entry built while
    # the setting was off (dev/changelog/597).
    if path == 'search.tag_id_cache_enabled':
        from ..channel_search import clear_tag_channel_ids_cache
        clear_tag_channel_ids_cache()

    # A shorter TTL should take effect immediately, not only once the last-built entry's
    # original (longer) TTL happens to expire on its own.
    if path == 'search.standing_breakdown_cache_ttl_seconds':
        from ..channel_search import clear_standing_breakdown_cache
        clear_standing_breakdown_cache()

    # The logo cache fetch job is registered only while the feature is on, so the toggle
    # has to move the job with it or turning the feature on would do nothing until the
    # next restart (dev/changelog/1056).
    if path == 'recording.logo_cache.enabled':
        from ..scheduler import apply_logo_cache_schedule
        apply_logo_cache_schedule()

    payload = {'success': True, 'restart_required': needs_restart,
               'changed_from_default': still_changed}
    gpu = _refresh_gpu_after_save(changed)
    if gpu is not None:
        from ..readiness import READY
        payload['gpu_trial'] = {'ok': gpu.status == READY,
                                'message': _gpu_trial_sentence(gpu)}
    return jsonify(payload)


@settings_bp.route('/api/settings/password', methods=['POST'])
def api_settings_password():
    """Set or change the login-gate password. Body: {current_password?, new_password,
    confirm_password}. current_password is required whenever the gate is actually live -
    never waived just because the caller is already logged in, since the whole-app gate
    has no step-up re-prompt for anything, this included.

    It *is* waived when the gate is inert (auth.enabled false, or no hash), because there
    is then no authentication boundary for it to protect: anyone who can reach this route
    can already rewrite every config value, post_script included. Requiring it there
    protected nothing and broke the documented recovery path - README.md's forgotten-
    password steps leave the old hash in place on purpose, and Settings then refused to
    accept a new password without the one the user had just declared forgotten
    (dev/changelog/487)."""
    from werkzeug.security import check_password_hash, generate_password_hash
    from ..auth import gate_active, password_epoch
    log = logging.getLogger(__name__)
    data = request.get_json(silent=True) or {}
    current_password = data.get('current_password') or ''
    new_password = data.get('new_password') or ''
    confirm_password = data.get('confirm_password') or ''

    if not new_password:
        return jsonify({'error': 'New password is required'}), 400
    if new_password != confirm_password:
        return jsonify({'error': 'New password and confirmation do not match'}), 400

    # One unit under the config write lock, like every load → mutate → save in this file:
    # a concurrent settings save that read before this one writes would otherwise restore
    # the old hash it had snapshotted (dev/docs/BUGS.md 2026-08-15 @ 05:32:07 PM ET).
    with config_write_lock:
        cfg, file_cfg = load_for_edit()
        existing_hash = (cfg.get('auth', {}).get('password_hash') or '').strip()
        if existing_hash and gate_active(cfg.get('auth') or {}):
            if not current_password or not check_password_hash(existing_hash, current_password):
                log.warning('Rejected password change: current password did not match')
                return jsonify({'error': 'Current password is incorrect'}), 400

        set_nested(file_cfg, 'auth.password_hash', generate_password_hash(new_password))
        save_config(file_cfg)

    from ..auth import refresh_auth
    refresh_auth(current_app._get_current_object())

    # Every session issued against the old password is now invalid (password_epoch) -
    # including this one. Re-stamp the browser that just did the change so it stays
    # signed in, which is what "signs out every *other* device" means. auth_at is
    # deliberately left alone: the absolute timeout still runs from the original login,
    # so changing a password cannot be used to extend a session indefinitely.
    if session.get('auth_ok'):
        session['auth_pw'] = password_epoch(current_app.config.get('AUTH') or {})

    log.info('Login gate password was %s', 'changed' if existing_hash else 'set')
    return jsonify({'success': True})


@settings_bp.route('/api/settings/ha-api-key', methods=['POST'])
def api_settings_ha_api_key():
    """Generate (or regenerate) the Home Assistant integration's API key. Same show-once/
    hash-only shape as api_settings_password: the plaintext is returned exactly once in
    this response and never stored or logged again - only its hash persists. Unlike a
    password there is nothing for the caller to confirm (it's generated, not typed), and
    no 'current key' check - this single-user app's Settings page is already the trust
    boundary (the same visitor could already rewrite post_script and run code)."""
    from werkzeug.security import generate_password_hash
    import secrets
    log = logging.getLogger(__name__)

    api_key = secrets.token_hex(32)
    with config_write_lock:
        cfg, file_cfg = load_for_edit()
        had_key = bool((cfg.get('integrations', {}).get('home_assistant', {})
                        .get('api_key_hash') or '').strip())
        set_nested(file_cfg, 'integrations.home_assistant.api_key_hash',
                   generate_password_hash(api_key))
        save_config(file_cfg)

    log.info('Home Assistant integration API key was %s', 'regenerated' if had_key else 'generated')
    return jsonify({'success': True, 'api_key': api_key})


@settings_bp.route('/api/settings/reveal')
def api_settings_reveal():
    """Return the real stored value of one sensitive config leaf, for the in-field reveal
    (eyeball) control. Gated to _is_sensitive_path leaves - the exact set the read surfaces
    mask - so it can only un-mask what was masked, never read an arbitrary config value.
    (No new secret exposure: this single-user app has no roles, and the login gate - when
    on - already covers this route like every other one; the same visitor can already
    edit every config value, the eyeball just reads back what they can already set.)"""
    path = request.args.get('path', '')
    if not _is_sensitive_path(path):
        return jsonify({'error': 'Not a revealable field'}), 400
    value = load_config()
    for key in path.split('.'):
        if not isinstance(value, dict) or key not in value:
            value = ''
            break
        value = value[key]
    return jsonify({'success': True, 'value': value if isinstance(value, str) else ''})


# Minted once per process, at import. The restart-wait modal holds the value the POST
# below answered with and reloads when a heartbeat reports a different one - proof the
# process was replaced, rather than an inference from catching a failed poll, which a
# container restart faster than one poll interval never produced (dev/changelog/1022).
_INSTANCE_ID = uuid.uuid4().hex


@settings_bp.route('/api/settings/restart-status')
def api_restart_status():
    return jsonify({'restart_needed': is_restart_needed(), 'instance_id': _INSTANCE_ID})


# What each blocking status means to a human deciding whether to restart anyway.
# Keyed by RESTART_BLOCKING_STATUSES; a status missing here falls back to the raw value
# rather than being silently dropped from the warning.
_BLOCKING_PHRASE = {
    REC_STATUS_IN_PROGRESS: 'is recording now',
    REC_STATUS_PAUSED: 'is paused mid-recording',
    REC_STATUS_RETRYING: 'is waiting to retry after a stream failure',
    REC_STATUS_CONCATENATING: 'is joining its segments',
    REC_STATUS_ANALYZING: 'is checking the joined file before converting it',
    REC_STATUS_CONVERTING: 'is converting to its final file',
}


def _restart_recordings():
    """Recordings in a blocking status, split ``(working, parked)`` on
    postprocess_waiting_since exactly as tools/check_busy.py splits them: a parked row is
    doing no work and does not block a restart (dev/changelog/952, 1026)."""
    from ..database import Recording, RESTART_BLOCKING_STATUSES
    rows = (Recording.query
            .filter(Recording.status.in_(RESTART_BLOCKING_STATUSES))
            .order_by(Recording.id)
            .all())
    return ([r for r in rows if r.postprocess_waiting_since is None],
            [r for r in rows if r.postprocess_waiting_since is not None])


def _restart_parked_rows(parked=None):
    """Parked recordings as the restart surfaces list them: not blocking, but named with
    what they wait on and what a restart costs them, in the CLI's own words."""
    from ..database import parked_restart_phrase
    if parked is None:
        parked = _restart_recordings()[1]
    return [{
        'id': r.id,
        'name': r.name,
        'status': r.status,
        'label': f'#{r.id} "{r.name}" is ' + parked_restart_phrase(
            r.status, r.postprocess_waiting_on_name, r.conversion_progress_pct),
    } for r in parked]


def _restart_blocking_rows(busy):
    """Everything a restart would interrupt, as the modal's row list.

    The same set tools/check_busy.py blocks on, so the two restart surfaces - this button
    and the CLI - never disagree about what is in flight. A parked recording is not in it;
    _restart_parked_rows() reports those. Both read database rows rather than
    app/admission.py's in-memory registry: the CLI runs in a separate process and cannot
    see it, and a surface that saw more than the other would be the harder thing to reason
    about. That registry's docstring is the authority on what it is for.

    Every row here is synthetic except a recording's - 'id' means "recording id" to the
    modal, so anything else sends None rather than an id from another table.
    """
    from ..database import OnDemandTestJob, ChannelTest, Account, EpgSource
    from ..search_index import rebuilding_index_names

    rows = [{
        'id': r.id,
        'name': r.name,
        'status': r.status,
        'label': f'#{r.id} "{r.name}" {_BLOCKING_PHRASE.get(r.status, r.status)}',
    } for r in busy]

    # A health check run spawns a real ffmpeg probe per channel and can run for hours; the
    # RUNNING row covers the whole run, including the waits between channels where no probe
    # is live. The per-channel tests underneath it carry its job_id, which is what keeps the
    # query below from counting the same run twice.
    rows += [{
        'id': None,
        'name': job.name,
        'status': 'RUNNING',
        'label': f'Health check "{job.name}" is running',
    } for job in OnDemandTestJob.query.filter_by(status='RUNNING')
                                .order_by(OnDemandTestJob.id).all()]

    # A pre-record check or a "Test now" click - one live probe, and no job row to report
    # it. Only one can exist at a time (the tester is single-run-globally), so touching
    # .channel per row is not an N+1 risk.
    rows += [{
        'id': None,
        'name': t.channel.name if t.channel else None,
        'status': 'TESTING',
        'label': f'A channel test on "{t.channel.name if t.channel else "a deleted channel"}"'
                 ' is running',
    } for t in ChannelTest.query.filter(ChannelTest.test_ended_at.is_(None),
                                        ChannelTest.job_id.is_(None))
                          .order_by(ChannelTest.id).all()]

    # Restarting mid-rebuild strands it at BUILDING rather than corrupting anything, but
    # still costs the rebuild having to start over and a window of degraded (LIKE-fallback)
    # search until it does.
    rows += [{
        'id': None,
        'name': name,
        'status': 'BUILDING',
        'label': f'Search index "{name}" is rebuilding',
    } for name in rebuilding_index_names()]

    # Blocking since dev/changelog/732 - see tools/check_busy.py::syncing_accounts for why
    # "safely cancellable" stopped being a reason to wave a sync through.
    rows += [{
        'id': None,
        'name': acc.name,
        'status': 'SYNCING',
        'label': f'Account "{acc.name}" is syncing',
    } for acc in Account.query.filter_by(status='SYNCING').order_by(Account.id).all()]

    # A source refreshing outside its owner's sync (tools/check_busy.py::
    # refreshing_epg_sources for why it blocks). One inside a sync is named by the account.
    rows += [{
        'id': None,
        'name': src.name,
        'status': 'REFRESHING',
        'label': f'EPG source "{src.name}" is refreshing',
    } for src in EpgSource.query.join(Account, Account.id == EpgSource.owner_account_id)
                              .filter(EpgSource.refresh_started_at.isnot(None),
                                      Account.status != 'SYNCING')
                              .order_by(EpgSource.id).all()]

    return rows


@settings_bp.route('/api/settings/restart-parked')
def api_restart_parked():
    """Parked recordings, for the Restart confirm to name before anyone clicks. They never
    make the POST below refuse, so without this a restart with only a parked conversion in
    flight would discard its partial encode with nothing said (dev/changelog/1026). Kept
    off restart-status, which every page polls."""
    return jsonify({'success': True, 'parked': _restart_parked_rows()})


@settings_bp.route('/api/settings/restart', methods=['POST'])
def api_restart_now():
    force = bool((request.get_json(silent=True) or {}).get('force'))
    # The blocking-list modal (static/js/maintenance.js::doRestart) renders whatever
    # 'blocking' sends it, so naming a new blocker here is the whole change on the JS side.
    working, parked = _restart_recordings()
    rows = _restart_blocking_rows(working)

    if rows and not force:
        return jsonify({
            'error': 'Work is in flight: ' + '; '.join(r['label'] for r in rows),
            'blocking': rows,
            'parked': _restart_parked_rows(parked),
        }), 409

    if os.environ.get('CHANNELBIN_DOCKER'):
        # Inside the container, docker/entrypoint.sh execs straight into this process -
        # it IS tini's monitored child, not a detached background service. Shelling out
        # to restart.sh would pkill that child, which makes tini exit and tears down the
        # whole container (the freshly-nohup'd replacement dies with it). Exit this
        # process directly instead and rely on the container's restart policy to bring
        # it back - the same SIGTERM path run.py's handler already uses when restart.sh
        # sends it outside a container.
        _schedule_self_restart()
        return jsonify({'success': True, 'forced': bool(rows), 'instance_id': _INSTANCE_ID})

    # This route is the enforcement point, so the script is always invoked with --force:
    # its own busy guard would otherwise refuse a restart already approved here, and the
    # refusal would be invisible (Popen output goes to DEVNULL and the exit code is never
    # collected - the UI would report a restart that never happened).
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    subprocess.Popen(
        ['bash', os.path.join(base_dir, 'restart.sh'), '--force'],
        cwd=base_dir,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return jsonify({'success': True, 'forced': bool(rows), 'instance_id': _INSTANCE_ID})


def _schedule_self_restart():
    """Send this process SIGTERM shortly after returning, so the response above has
    time to reach the client first. run.py's own handler kills live captures/conversions
    before the process exits."""
    threading.Timer(0.5, os.kill, args=(os.getpid(), signal.SIGTERM)).start()


# ---------------------------------------------------------------------------
# Backup API
# ---------------------------------------------------------------------------

@settings_bp.route('/api/settings/backups')
def api_backups_list():
    from ..config_backup import list_backups
    return jsonify(list_backups())


@settings_bp.route('/api/settings/backup', methods=['POST'])
def api_backup_now():
    from ..config_backup import do_backup
    try:
        path = do_backup()
        return jsonify({'success': True, 'filename': os.path.basename(path)})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@settings_bp.route('/api/settings/diff/<path:filename>')
def api_backup_diff(filename):
    from ..config_backup import get_diff, list_backups
    # Validate the file is actually in the backup dir
    backups = {b['filename']: b['path'] for b in list_backups()}
    if filename not in backups:
        return jsonify({'error': 'Backup not found'}), 404

    try:
        diff_lines = redact_sensitive_diff_lines(get_diff(backups[filename]))
        return jsonify({'diff': diff_lines})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@settings_bp.route('/api/settings/rollback', methods=['POST'])
def api_rollback():
    from ..config_backup import apply_backup, list_backups
    data = request.get_json(silent=True) or {}
    filename = data.get('filename', '')

    backups = {b['filename']: b['path'] for b in list_backups()}
    if filename not in backups:
        return jsonify({'error': 'Backup not found'}), 404

    try:
        apply_backup(backups[filename])
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    # A restored config can carry a different recording.logo_cache.enabled, and the job is
    # registered only while the feature is on - so this path has to move the job too, or a
    # rollback that turns logo caching on leaves it doing nothing with nothing saying why
    # (dev/changelog/1056). The other live-config hooks this route does not fire (auth,
    # toolchain, the search caches) are a pre-existing gap and are not widened here.
    from ..scheduler import apply_logo_cache_schedule
    apply_logo_cache_schedule()
    return jsonify({'success': True})


@settings_bp.route('/api/settings/support-bundle')
def api_support_bundle():
    """Sanitized diagnostic zip, assembled fresh on every request (DESIGN-secrets.md §7)
    - never a copy of dvr.db or raw config.yaml.

    `?names=1` is the user's explicit opt-in to shipping real account and channel names
    instead of pseudonyms (dev/changelog/840). It defaults to off, and the download name
    says which mode produced the file so an opted-in bundle is identifiable after it has
    been saved, mailed or renamed away from the page that produced it."""
    from datetime import datetime
    from ..support_bundle import build_support_bundle
    include_names = request.args.get('names') == '1'
    try:
        data = build_support_bundle(include_names=include_names)
    except Exception as exc:
        return jsonify({'error': f'Support bundle export failed: {exc}'}), 500
    ts = datetime.now(tz=get_display_tz()).strftime('%Y-%m-%d-%H-%M-%S')
    suffix = '-with-names' if include_names else ''
    return send_file(
        BytesIO(data), mimetype='application/zip', as_attachment=True,
        download_name=f'channelbin-support-{ts}{suffix}.zip')


# ---------------------------------------------------------------------------
# Search index maintenance
# ---------------------------------------------------------------------------

# What each index actually backs, in the user's terms - the raw names ('channels',
# 'programs') say nothing about which search box goes slow when one is unusable.
_SEARCH_INDEX_LABELS = {
    'channels': 'Channels (names, groups, EPG titles)',
    'programs': 'Programs (upcoming airings)',
}


@settings_bp.route('/api/settings/search-index')
def api_search_index_status():
    """Per-index state for the Settings panel: is search running indexed or degraded, and why.

    The only surface that renders search_index_state at all. Before this the degradation was
    knowable only from an alert firing, which is the 'nothing silent' principle inverted -
    a stale index produces correct-but-slow searches and says nothing (dev/changelog/426).

    `ready` is asked per index through search_index_readiness() rather than inferred from
    `status`, because OK-but-stale is a real and common state that the status column alone
    reports as healthy.
    """
    from ..database import SearchIndexState
    from ..search_index import (SEARCH_INDEX_NAMES, rebuild_in_progress,
                                search_index_readiness)

    rows = {r.name: r for r in
            SearchIndexState.query.filter(SearchIndexState.name.in_(SEARCH_INDEX_NAMES))}
    indexes = []
    for name in SEARCH_INDEX_NAMES:
        state = rows.get(name)
        ready, reason = search_index_readiness(name)
        indexes.append({
            'name': name,
            'label': _SEARCH_INDEX_LABELS.get(name, name),
            'status': state.status if state else 'NEVER_BUILT',
            'row_count': state.row_count if state else None,
            'duration_ms': state.duration_ms if state else None,
            'error': (state.error or None) if state else None,
            # Formatted server-side: the display timezone lives in config and JS must never
            # name a zone of its own (CLAUDE.md Timezones).
            'rebuilt_at': format_local(state.rebuilt_at if state else None,
                                       'short_datetime', none_value=None),
            'ready': ready,
            'reason': reason,
        })
    return jsonify({'success': True, 'indexes': indexes,
                    'rebuilding': rebuild_in_progress()})


@settings_bp.route('/api/settings/search-index/rebuild', methods=['POST'])
def api_search_index_rebuild():
    """Rebuild both search indexes now, off the request thread.

    Until this existed, rebuild_search_indexes() had exactly one caller - the account sync
    close-out - so repairing a FAILED or stranded index meant waiting for a full sync while
    every search took the unindexed scan over 1.9M rows. That scan is what pegged both cores
    for 16 minutes on 2026-08-01.

    Never blocks the request: a programs rebuild measured 76.1s at 411k rows. The worker has
    no request context, so WorkloadRoutedSession routes it to the background connection pool
    and it cannot take a connection UI traffic needs. rebuild_search_indexes() never raises
    and owns its own retry_on_locked commits and alerting, so there is nothing to catch here.
    """
    from ..database import Account
    from ..search_index import rebuild_in_progress, rebuild_search_indexes

    if rebuild_in_progress():
        return jsonify({'error': 'A search index rebuild is already running.'}), 409

    # Refused rather than queued: a sync ends with its own rebuild, so a manual one started
    # mid-sync spends ~76s of CPU on an index the sync's next insert makes stale - during the
    # exact window this whole batch exists to keep quiet.
    syncing = Account.query.filter_by(status='SYNCING').first()
    if syncing is not None:
        return jsonify({'error': (
            f'"{syncing.name}" is syncing right now, and a sync rebuilds the search indexes '
            'when it finishes. Wait for it rather than rebuilding twice.')}), 409

    app_obj = current_app._get_current_object()

    def _worker():
        with app_obj.app_context():
            rebuild_search_indexes('manual rebuild from Maintenance')

    threading.Thread(target=_worker, daemon=True, name='search-index-rebuild').start()
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# The filename template designer (DESIGN.md 15.1/15.4; rollout dev/changelog/441)
#
# The designer is a reusable component rather than a page, so its data comes from
# these three JSON endpoints rather than from a template's context. `/settings/template`
# and templates/template_editor.html were deleted outright in the same change
# (DESIGN.md 11.4: a losing spelling gets deleted, not switched off).
# ---------------------------------------------------------------------------

#: The synthetic program the designer previews against when the user has picked no real
#: airing. Deliberately carries `2160p` in its title: the tag-cleanup rules below it need
#: something to act on, and a sample that demonstrates nothing teaches nothing.
_SAMPLE_PROGRAM_TITLE = 'The Tonight Show 2160p (backup)'


def _sample_program():
    """The built-in sample, with each configured tag's first pattern appended to the title.

    The appended patterns are the point, not noise: without them the two tag-cleanup
    pickers would render a rule list describing substitutions the preview never performs,
    which is the class of quiet mismatch DESIGN.md 15.4 exists to prevent.
    """
    from ..database import Tag
    from datetime import datetime, timedelta
    now = datetime.utcnow().replace(second=0, microsecond=0)
    start = now + timedelta(hours=9, minutes=23)
    title = _SAMPLE_PROGRAM_TITLE
    tag_patterns = [t.patterns[0].pattern for t in Tag.query.order_by(Tag.name).all()
                    if t.patterns]
    if tag_patterns:
        title = title + ' ' + ' '.join(tag_patterns)
    return {
        'title': title,
        'sub_title': '',
        'description': 'Late night talk show with celebrity guests and musical performances.',
        'channel_name': 'NBC',
        'category': 'Talk Show',
        'start_time': start,
        'stop_time': start + timedelta(hours=1),
    }


def _custom_program(args):
    """A program typed into the designer's `Custom` fields. Nothing here is persisted.

    The typed date/start/end are wall-clock values in the display timezone, same as every
    other datetime field in the app - so they are converted to naive UTC here and stored in
    the program dict the same way a real EPGEntry's start_time/stop_time would be. That is
    what lets render_filename_template's own local-timezone conversion produce back exactly
    what was typed, without this function needing to know or care what timezone is
    configured (dev/docs/BUGS.md 2026-08-05).
    """
    from datetime import datetime, timedelta

    def _wall(day, clock, fallback):
        try:
            return datetime.strptime(f'{day} {clock}', '%Y-%m-%d %H:%M')
        except ValueError:
            return fallback

    now_local = to_local(datetime.utcnow()).replace(tzinfo=None, second=0, microsecond=0)
    day = args.get('date', '') or now_local.strftime('%Y-%m-%d')
    start = _wall(day, args.get('start', '') or '00:00', now_local)
    stop = _wall(day, args.get('end', '') or args.get('start', '') or '00:00', start)
    # A show that ends after midnight ends on the NEXT day. Without this an 11:30pm-12:30am
    # program renders {end_time} an hour before {start_time}, which is not a state the user
    # can reach any other way.
    if stop < start:
        stop = stop + timedelta(days=1)
    return {
        'title': args.get('title', ''),
        'sub_title': args.get('sub_title', ''),
        'description': args.get('description', ''),
        'channel_name': args.get('channel', ''),
        'category': args.get('category', ''),
        'start_time': to_naive_utc(start),
        'stop_time': to_naive_utc(stop),
    }


def _preview_program(args):
    """(program dict, subject dict) for the requested source.

    The subject travels back with the preview rather than being assembled on the page,
    because DESIGN.md 15.7 requires the filename and the line describing what it is a
    filename FOR to come from one computation - two of them can disagree about which
    program is being previewed, and the whole screen is then lying.

    An `epg` source whose row is gone falls back to the sample and says so in
    `subject.fell_back`, so a stale picked airing degrades loudly rather than silently
    renaming what the user is looking at.
    """
    src = args.get('src', 'sample')
    fell_back = False
    if src == 'custom':
        program = _custom_program(args)
    elif src == 'epg':
        from ..database import db, EPGEntry
        entry = db.session.get(EPGEntry, args.get('epg_id', type=int) or 0)
        if entry is not None and entry.channel is not None:
            program = {
                'title': entry.title or '',
                'sub_title': entry.sub_title or '',
                'description': entry.description or '',
                'channel_name': entry.channel.name,
                'category': entry.category or '',
                'start_time': entry.start_time,
                'stop_time': entry.stop_time,
            }
        else:
            src, fell_back = 'sample', True
            program = _sample_program()
    else:
        src = 'sample'
        program = _sample_program()

    start = program.get('start_time')
    return program, {
        'source': src,
        'fell_back': fell_back,
        'title': program.get('title', ''),
        'channel': program.get('channel_name', ''),
        'start_time': start.strftime('%Y-%m-%dT%H:%M:%S') if start else '',
    }


def _unknown_tokens(template, tag_names):
    """Tokens that will land in the filename literally because nothing substitutes them.

    Resolved server-side because the server owns both halves of the answer - the variable
    registry in app/accounts.py and the Tag rows - and a browser-side copy of either is a
    second source of truth for what is spelled correctly.
    """
    known = {v for v, _ in TEMPLATE_VARIABLES}
    unknown = [t for t in re.findall(r'\{[a-z_]+\}', template) if t not in known]
    unknown += ['{tag:' + m.group(1) + '}' for m in _TAG_TOKEN_RE.finditer(template)
                if m.group(1) not in tag_names]
    # Deduplicated, order preserved: a template repeating one typo should say it once.
    return list(dict.fromkeys(unknown))


@settings_bp.route('/api/filename-designer')
def filename_designer_boot_api():
    """Everything the designer needs to open, in one request.

    One payload rather than template context, because the component is meant to open from
    any page (a Recording Profile's template field is the next host) and a page-supplied
    boot block would have to be duplicated on each of them.
    """
    from ..database import Tag
    cfg = load_config()
    rec = cfg.get('recording', {})
    tags = [{'name': t.name, 'color': t.color,
             'patterns': [p.pattern for p in t.patterns]}
            for t in Tag.query.order_by(Tag.name).all()]
    return jsonify({
        'success': True,
        'template': rec.get('filename_template', config_default('recording.filename_template')),
        'remove': list(rec.get('filename_tags_remove', []) or []),
        'replace': list(rec.get('filename_tags_replace', []) or []),
        'tags': tags,
        'variables': [{'name': v, 'desc': d} for v, d in TEMPLATE_VARIABLES],
        'tag_variables': [{'name': v, 'desc': d} for v, d in tag_template_variables()],
        # The designer draws the extension after the filename, so it has to be the one the
        # file will actually carry. Capture always writes .ts; post-processing remuxes it
        # to post_process.format, and with post-processing off the .ts is what survives.
        # Hardcoding .mp4 here would show an extension half of this app's configurations
        # never produce, which is the same defect as the filename itself being wrong.
        'extension': (rec.get('post_process', {}).get('format', 'mp4')
                      if rec.get('post_process', {}).get('enabled', True) else 'ts'),
    })


@settings_bp.route('/api/template-preview')
def template_preview_api():
    """Render a filename template against a sample, a real airing, or typed-in fields.

    Returns BOTH the rendered name and `disk` - the name after app/recorder.py::_safe_name,
    which is the string that actually lands in /dvr. The designer shows only `disk`
    (DESIGN.md 15.4): the old editor previewed the rendered name, which becomes the
    recording's NAME and never its filename, so it promised
    `2026-08-02 - Arsenal v Man City - NBC.mp4` for a file written as
    `2026-08-02_-_Arsenal_v_Man_City_-_NBC.mp4`. dev/docs/BUGS.md 2026-08-03.

    _safe_name stays the only implementation of that rule - the preview is computed here
    rather than in JS precisely so a second spelling of it cannot drift out of step with
    the one the recorder uses.
    """
    template = request.args.get('template', '')
    if not template.strip():
        return jsonify({'error': 'Template cannot be empty.'}), 400

    from ..database import Tag
    program, subject = _preview_program(request.args)
    tag_cleanup = (
        [(n, 'remove') for n in request.args.getlist('remove')] +
        [(n, 'replace') for n in request.args.getlist('replace')]
    )
    tz = get_display_tz()
    name = render_filename_template(template, program, tag_cleanup=tag_cleanup, tz=tz)
    disk = _safe_name(name)
    tag_names = {t.name for t in Tag.query.all()}
    # `also` renders extra templates against the SAME program and the SAME cleanup lists,
    # in the same request. It exists for the `Start from an example` menu, which shows what
    # each example would produce: computing those anywhere else would be a second renderer
    # that can disagree with the live preview about the very thing being compared. Bounded
    # at 8 so a hand-built request cannot turn one preview into an unbounded render loop.
    alsos = [{'template': t, 'disk': _safe_name(
        render_filename_template(t, program, tag_cleanup=tag_cleanup, tz=tz))}
        for t in request.args.getlist('also')[:8] if t.strip()]
    return jsonify({
        'success': True,
        'name': name,
        'disk': disk,
        'also': alsos,
        # The note under the filename appears only when the substitution actually changed
        # something, so a template that survives _safe_name intact carries no sentence
        # explaining a substitution that did not happen (DESIGN.md 15.4).
        'changed': disk != name,
        'unknown': _unknown_tokens(template, tag_names),
        'subject': subject,
    })


@settings_bp.route('/api/filename-template', methods=['POST'])
def filename_template_save_api():
    """Persist the template and the two tag-cleanup lists.

    Replaces the old form POST to /settings/template. A name in both lists is kept in
    `remove` only - the two modes are mutually exclusive by construction, and the control
    enforces it, but a hand-built request must not be able to store a state the designer
    cannot render.
    """
    data = request.get_json(silent=True) or {}
    template = (data.get('template') or '').strip()
    if not template:
        return jsonify({'error': 'Template cannot be empty.'}), 400
    remove = sorted({str(n) for n in data.get('remove', []) if n})
    replace = sorted({str(n) for n in data.get('replace', []) if n} - set(remove))

    with config_write_lock:
        # The raw FILE dict, not the merged config: writing load_config()'s result back would
        # bake every default into config.yaml as if the user had chosen it.
        cfg = _load_config_file() or {}
        cfg.setdefault('recording', {})
        cfg['recording']['filename_template'] = template
        cfg['recording']['filename_tags_remove'] = remove
        cfg['recording']['filename_tags_replace'] = replace
        save_config(cfg)
        still_changed = 'recording.filename_template' in changed_from_default(load_config())
    return jsonify({'success': True, 'template': template,
                    'remove': remove, 'replace': replace,
                    'changed_from_default': still_changed})


# ---------------------------------------------------------------------------
# Notifications settings
# ---------------------------------------------------------------------------

@settings_bp.route('/settings/notifications', methods=['GET'])
def notifications_settings():
    """The Notifications surface (DESIGN.md 15.2/15.6; rollout dev/changelog/440).

    The page is rendered from one boot payload rather than from several template
    variables, because 15.7 requires a single writer for the whole services region -
    the add bar, the cards and the empty state - and the heading's count and the
    cards below it must come from one computation. A Jinja first paint plus a JS
    re-render after every add/remove is two writers, which is the thing that rule
    forbids.
    """
    # Broken-URL detection needs the raw value (a service's URL equal to its own
    # placeholder hint), which the masked payload below can no longer tell apart from a
    # real credential - so compute it before masking, and pass only the resulting boolean
    # through (never the raw url itself).
    raw_services = load_config().get('notifications', {}).get('services', {})

    # Mask so the service URL inputs render MASK_SENTINEL instead of the real webhook token;
    # the sentinel round-trips back on save via api_notifications_service_save -> save_config.
    cfg = mask_config(load_config())
    notif_cfg = cfg.get('notifications', {})
    services = notif_cfg.get('services', {})
    routing = notif_cfg.get('routing', {})

    # DESIGN.md 15.2: added and enabled are two different things, and `added` needs no
    # new config key - it is "enabled, or a URL is stored", both of which
    # notifications.services.<name> already carries. A service that is configured and
    # then paused therefore keeps its card, and its URL.
    boot_services = {}
    for name, label in SERVICE_LABELS.items():
        scfg = services.get(name, {})
        url = scfg.get('url') or ''
        raw_url = (raw_services.get(name, {}) or {}).get('url') or ''
        boot_services[name] = {
            'label': label,
            'hint': SERVICE_URL_HINTS.get(name, ''),
            'enabled': bool(scfg.get('enabled')),
            'url': url,
            'added': bool(scfg.get('enabled')) or bool(url),
            # A service enabled with its own placeholder hint stored as the URL - see
            # api_notifications_service_save, which rejects this going forward, and
            # notifications.py's create-or-dismiss alert for the same condition.
            'broken': bool(scfg.get('enabled')) and raw_url == SERVICE_URL_HINTS.get(name),
            # None = no override, inherits push_rate_limit_seconds; 0 = unlimited.
            'rate_limit_seconds': scfg.get('rate_limit_seconds'),
        }

    # Merge routing with ALERT_TYPES so every known type appears - except the retired ones,
    # which nothing raises, so a row for them would be a switch that cannot do anything
    # (dev/changelog/928). Their existing rows still render everywhere alerts are shown.
    full_routing = {}
    for key, meta in ALERT_TYPES.items():
        if key in RETIRED_ALERT_TYPES:
            continue
        r = routing.get(key, {})
        full_routing[key] = {
            'label': meta['label'],
            'severity': meta['severity'],
            'in_app': r.get('in_app', True),
            'push_services': [s for s in r.get('push_services', []) if s in SERVICE_LABELS],
        }

    return render_template(
        'notifications_settings.html',
        boot={
            'services': boot_services,
            'routing': full_routing,
            'rate_limit': notif_cfg.get('push_rate_limit_seconds', 60),
            'base_url': notif_cfg.get('base_url', ''),
        },
    )


@settings_bp.route('/api/notifications/services/<name>', methods=['POST'])
def api_notifications_service_save(name):
    if name not in SERVICE_LABELS:
        return jsonify({'error': 'Unknown service'}), 400
    data = request.get_json(silent=True) or {}
    with config_write_lock:
        cfg, file_cfg = load_for_edit()
        svc = file_cfg.setdefault('notifications', {}).setdefault('services', {}).setdefault(name, {})
        if 'enabled' in data:
            svc['enabled'] = bool(data['enabled'])
        if 'url' in data:
            new_url = str(data['url']).strip()
            if new_url == SERVICE_URL_HINTS.get(name):
                # dev/docs/BUGS.md 2026-08-11 09:00 PM: that literal string is the field's own
                # example text, never a real credential - saving it silently kills every push
                # to this service. Reject server-side, not just in the JS auto-save handler.
                return jsonify({
                    'error': f"That's the example placeholder for {SERVICE_LABELS[name]}, not "
                             'a real credential - paste the actual Apprise URL instead.'
                }), 400
            svc['url'] = new_url
        if 'rate_limit_seconds' in data:
            raw = data['rate_limit_seconds']
            if raw is None or raw == '':
                svc.pop('rate_limit_seconds', None)
            else:
                try:
                    svc['rate_limit_seconds'] = max(0, int(raw))
                except (TypeError, ValueError):
                    return jsonify({'error': 'Invalid rate limit'}), 400
        save_config(file_cfg)
        # `svc` is the raw file entry, which carries only the leaves the user has actually
        # saved - so read the placeholder condition off the effective service instead, or a
        # leaf still sitting on its default would look absent rather than false/empty.
        effective_svc = {**(cfg.get('notifications', {}).get('services', {}).get(name) or {}),
                         **svc}
    # Outside the lock: this writes to the database, and no path may hold the config lock
    # while taking a DB lock (or the two could be acquired in opposite orders elsewhere).
    if not (bool(effective_svc.get('enabled'))
            and effective_svc.get('url', '') == SERVICE_URL_HINTS.get(name)):
        dismiss_placeholder_url_alert(name)
    # The service has just been reconfigured, so a standing "its sends are failing" claim
    # describes the old settings. It re-raises on the next failed flush if it still holds;
    # leaving it up is what would strand a deliberately-disabled service accused forever,
    # since a disabled service never produces the successful send that clears it.
    dismiss_send_failure_alert(name)
    return jsonify({'success': True})


@settings_bp.route('/api/notifications/services/<name>', methods=['DELETE'])
def api_notifications_service_remove(name):
    """Remove a service: clear its URL, disable it, and drop it from every routing row.

    One save, not three requests. Teardown has to release everything the create path
    acquired (CLAUDE.md defect-class rules) - a removal that left the service selected
    in half the routing rows would silently come back the moment it was re-added, and
    a URL left behind is a credential the page claims it deleted.
    """
    if name not in SERVICE_LABELS:
        return jsonify({'error': 'Unknown service'}), 400
    with config_write_lock:
        file_cfg = _load_config_file() or {}
        notif = file_cfg.setdefault('notifications', {})
        notif.setdefault('services', {})[name] = {'enabled': False, 'url': ''}
        # The raw file's routing rows are the only ones that can name this service: a row
        # still on its default carries an empty push_services list by construction, so
        # there is nothing in the merged-but-unsaved half for this pass to miss. get(),
        # not setdefault() - a file with no routing block of its own must not gain an
        # empty one just because a service was removed.
        for row in (notif.get('routing') or {}).values():
            if isinstance(row, dict) and name in (row.get('push_services') or []):
                row['push_services'] = [s for s in row['push_services'] if s != name]
        save_config(file_cfg)
    dismiss_placeholder_url_alert(name)  # DB write - never under the config lock
    dismiss_send_failure_alert(name)
    return jsonify({'success': True})


@settings_bp.route('/api/notifications/services/<name>/test', methods=['POST'])
def api_notifications_service_test(name):
    if name not in SERVICE_LABELS:
        return jsonify({'error': 'Unknown service'}), 400
    from ..notifications import send_test
    ok, err = send_test(name)
    if not ok:
        return jsonify({'error': err or 'Test notification failed'}), 500
    return jsonify({'success': True})


@settings_bp.route('/api/notifications/routing', methods=['POST'])
def api_notifications_routing_save():
    data = request.get_json(silent=True) or {}
    with config_write_lock:
        file_cfg = _load_config_file() or {}
        routing = file_cfg.setdefault('notifications', {}).setdefault('routing', {})
        for alert_type, row in data.items():
            if alert_type not in ALERT_TYPES:
                continue
            routing[alert_type] = {
                'in_app': bool(row.get('in_app', True)),
                'push_services': [s for s in (row.get('push_services') or [])
                                  if s in SERVICE_LABELS],
            }
        save_config(file_cfg)
    return jsonify({'success': True})


@settings_bp.route('/api/notifications/rate-limit', methods=['POST'])
def api_notifications_rate_limit():
    data = request.get_json(silent=True) or {}
    try:
        seconds = max(1, int(data.get('seconds', 60)))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid value'}), 400
    with config_write_lock:
        file_cfg = _load_config_file() or {}
        file_cfg.setdefault('notifications', {})['push_rate_limit_seconds'] = seconds
        save_config(file_cfg)
    return jsonify({'success': True})
