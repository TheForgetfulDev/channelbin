from flask import Blueprint, render_template, request, jsonify

from .. import db
from ..channel_tester import resolve_health_check_settings
from ..config import load_config
from ..database import HealthCheckProfile, OnDemandTestJob
from ..db_utils import retry_on_locked
from ..profile_forms import (BOOL, INT, TEXT, ProfileField, default_summary,
                             nullable_overrides, parse_profile_body, profile_payload)

health_check_profiles_bp = Blueprint('health_check_profiles', __name__)

# Every editable field, in the order the modal renders them. Each label matches the
# field's on-screen label so a validation error reads the same on both sides. Every
# override here is nullable - blank means "inherit the channel_testing.* config value" -
# so none of them declares a blank_value. See app/profile_forms.py for what that means.
_FIELDS = (
    ProfileField('name', 'Name', TEXT, required=True),
    ProfileField('test_duration_seconds', 'Test duration', INT),
    ProfileField('wait_between_channels_seconds', 'Wait between channels', INT),
    ProfileField('screenshots_enabled', 'Capture screenshots', BOOL),
    ProfileField('connect_retries', 'Connect retries', INT),
    ProfileField('connect_timeout_seconds', 'Connect timeout', INT),
    ProfileField('connect_retry_delay_seconds', 'Retry delay', INT),
)


def _profile_payload(p):
    return profile_payload(p, _FIELDS)


def _read_profile_body():
    """(values, error) parsed from the JSON body. The modal runs the same checks before
    it submits, but this is the one that counts: presentation-layer validation is never
    the enforcement point (CLAUDE.md - enforcement lives server-side)."""
    return parse_profile_body(_FIELDS, request.get_json(silent=True) or {})


# The row shows what a profile CHANGES; anything it inherits is absent, and the globals
# are stated once under the list instead of once per row (dev/changelog/864). Labels are
# the ones the retired columns carried, so the words do not move for a returning user.
# Every field here is nullable, so unlike the recording profiles there is no NOT NULL
# column to append by hand.
_OVERRIDE_ROWS = (
    ('test_duration_seconds', 'Test duration', lambda v: f'{v}s'),
    ('wait_between_channels_seconds', 'Wait between', lambda v: f'{v}s'),
    ('screenshots_enabled', 'Screenshots', lambda v: 'On' if v else 'Off'),
    ('connect_retries', 'Connect retries', str),
    ('connect_timeout_seconds', 'Connect timeout', lambda v: f'{v}s'),
    ('connect_retry_delay_seconds', 'Retry delay', lambda v: f'{v}s'),
)


@health_check_profiles_bp.route('/health-check-profiles')
def health_check_profiles_list():
    profiles = HealthCheckProfile.query.order_by(HealthCheckProfile.name).all()
    job_counts = dict(
        db.session.query(OnDemandTestJob.profile_id, db.func.count(OnDemandTestJob.id))
        .filter(OnDemandTestJob.profile_id.isnot(None))
        .group_by(OnDemandTestJob.profile_id)
        .all()
    )
    cfg = load_config()
    global_ct = cfg.get('channel_testing', {})
    # The modal's "Default (N)" hints and the list's defaults line read the same resolved
    # dict, so the two surfaces cannot disagree about what unset means.
    # resolve_health_check_settings is the one place those fallbacks are spelled.
    defaults = resolve_health_check_settings(global_ct, None)
    return render_template(
        'health_check_profiles.html',
        profiles=profiles,
        job_counts=job_counts,
        global_ct=global_ct,
        defaults=defaults,
        # Built here rather than in the template: the "did the user set this" test is
        # `is not None`, and a Jinja conditional written per field is where that quietly
        # becomes truthiness and starts hiding a profile's 0 or False
        # (app/profile_forms.py).
        overrides={p.id: nullable_overrides(p, _OVERRIDE_ROWS) for p in profiles},
        default_summary=default_summary(defaults, _OVERRIDE_ROWS),
        profiles_json=[_profile_payload(p) for p in profiles],
    )


@health_check_profiles_bp.route('/api/health-check-profiles', methods=['POST'])
def create_health_check_profile():
    values, error = _read_profile_body()
    if error:
        return jsonify({'error': error}), 400

    @retry_on_locked()
    def _create_and_commit():
        profile = HealthCheckProfile(**values)
        db.session.add(profile)
        db.session.commit()
        return profile

    profile = _create_and_commit()
    return jsonify({'success': True, 'profile': _profile_payload(profile)})


@health_check_profiles_bp.route('/api/health-check-profiles/<int:profile_id>', methods=['PUT'])
def update_health_check_profile(profile_id):
    if db.session.get(HealthCheckProfile, profile_id) is None:
        return jsonify({'error': 'Profile not found'}), 404

    values, error = _read_profile_body()
    if error:
        return jsonify({'error': error}), 400

    @retry_on_locked()
    def _update_and_commit():
        # Re-fetched inside the closure, not captured outside it: a rolled-back retry
        # expires the instance's pending changes, so replaying setattr on a row fetched
        # before the retry would silently persist nothing (CLAUDE.md retry_on_locked).
        profile = db.session.get(HealthCheckProfile, profile_id)
        if profile is None:
            return None
        for field, value in values.items():
            setattr(profile, field, value)
        db.session.commit()
        return profile

    profile = _update_and_commit()
    if profile is None:
        return jsonify({'error': 'Profile not found'}), 404
    return jsonify({'success': True, 'profile': _profile_payload(profile)})


@health_check_profiles_bp.route('/api/health-check-profiles/<int:profile_id>', methods=['DELETE'])
def delete_health_check_profile(profile_id):
    profile = db.session.get(HealthCheckProfile, profile_id)
    if profile is None:
        return jsonify({'error': 'Profile not found'}), 404
    name = profile.name

    # Two separate commits (unlink references, then delete the row) each need their
    # own retry_on_locked closure - see CLAUDE.md's db.session.commit() rule: a retry
    # that re-ran both commits from one decorator could re-delete-and-recreate state
    # incorrectly if the second commit failed after the first already succeeded.
    @retry_on_locked()
    def _unlink_references():
        OnDemandTestJob.query.filter_by(profile_id=profile_id).update({'profile_id': None})
        db.session.commit()

    @retry_on_locked()
    def _delete_profile_row():
        p = db.session.get(HealthCheckProfile, profile_id)
        if p is not None:
            db.session.delete(p)
            db.session.commit()

    _unlink_references()
    _delete_profile_row()

    return jsonify({'success': True, 'name': name})
