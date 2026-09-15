from flask import Blueprint, render_template, request, jsonify

from .. import db
from ..config import load_config
from ..database import RecordingProfile, Recording, Channel
from ..db_utils import retry_on_locked
from ..profile_forms import (BOOL, INT, TEXT, Override, ProfileField, default_summary,
                             nullable_overrides, parse_profile_body, profile_payload)

profiles_bp = Blueprint('profiles', __name__)

# Every editable field, in the order the modal renders them. Each label matches the
# field's on-screen label so a validation error reads the same on both sides.
#
# Two fields are deliberately NOT plain "blank means inherit" (app/profile_forms.py):
#   - the padding columns are NOT NULL with a 0 default, so they declare blank_value=0
#   - retention_days IS nullable, and its 0 is the opposite of its None: None = use the
#     global retention window, 0 = never auto-delete even when a global window is set
_FIELDS = (
    ProfileField('name', 'Name', TEXT, required=True),
    ProfileField('filename_template', 'Filename template', TEXT),
    ProfileField('pre_padding_minutes', 'Pre-record padding', INT, blank_value=0),
    ProfileField('post_padding_minutes', 'Post-record padding', INT, blank_value=0),
    ProfileField('stall_timeout_seconds', 'Stall timeout', INT),
    ProfileField('restart_delay_seconds', 'Restart delay', INT),
    ProfileField('max_consecutive_failures', 'Max consecutive failures', INT),
    ProfileField('stall_move_count', 'Stalls before moving on', INT),
    ProfileField('stall_move_window_minutes', 'Stall window', INT),
    ProfileField('retention_days', 'Auto-delete after', INT),
    ProfileField('pre_check_enabled', 'Pre-recording health check', BOOL),
)


def _profile_payload(p):
    return profile_payload(p, _FIELDS)


def _read_profile_body():
    """(values, error) parsed from the JSON body. The modal runs the same checks before
    it submits, but this is the one that counts: presentation-layer validation is never
    the enforcement point (CLAUDE.md - enforcement lives server-side)."""
    return parse_profile_body(_FIELDS, request.get_json(silent=True) or {})


def _global_defaults(cfg):
    """What each inheritable field falls back to, keyed by field. Read straight off the
    merged config rather than re-spelling any literal: load_config() always merges
    _DEFAULTS, so every key here is guaranteed present, and app/config.py stays the one
    place those numbers are written down. The table's "Default (N)" cells and the modal's
    hints both render this, so the two surfaces cannot disagree.

    The padding fields are absent on purpose - they are NOT NULL columns that default to
    0 and inherit nothing, so there is no global to quote.
    """
    return {
        'filename_template': cfg['recording']['filename_template'],
        'stall_timeout_seconds': cfg['watchdog']['stall_timeout_seconds'],
        'restart_delay_seconds': cfg['watchdog']['restart_delay_seconds'],
        'max_consecutive_failures': cfg['watchdog']['max_consecutive_failures'],
        'stall_move_count': cfg['watchdog']['stall_move_count'],
        'stall_move_window_minutes': cfg['watchdog']['stall_move_window_minutes'],
        'retention_days': cfg['recording']['retention_days'],
        'pre_check_enabled': cfg['channel_testing']['pre_check']['enabled'],
    }


def _retention(value):
    """Shared by an override cell and the defaults line, so the two cannot spell the same
    window differently. Falsy means "nothing is ever deleted" in BOTH readings, which is
    why truthiness is safe here and only here: a profile's 0 means never auto-delete, and
    an unset global means there is no window to inherit. Which of the two a caller is
    looking at is decided before this runs - nullable_overrides() tests `is not None`."""
    if not value:
        return 'Never'
    return f'{value} day' if value == 1 else f'{value} days'


# The row shows what a profile CHANGES; anything it inherits is absent, and the globals
# are stated once under the list instead of once per row (dev/changelog/864). Labels are
# the ones the retired columns carried, so the words do not move for a returning user.
# `filename_template` is deliberately not here - it is the one override too long for a
# pair, and gets its own line under the name.
_OVERRIDE_ROWS = (
    ('stall_timeout_seconds', 'Stall timeout', lambda v: f'{v}s'),
    ('restart_delay_seconds', 'Restart delay', lambda v: f'{v}s'),
    ('max_consecutive_failures', 'Max failures', str),
    ('stall_move_count', 'Stalls before moving on', str),
    ('stall_move_window_minutes', 'Stall window', lambda v: f'{v}m'),
    ('retention_days', 'Auto-delete', _retention),
    ('pre_check_enabled', 'Pre-recording check', lambda v: 'On' if v else 'Off'),
)


def _overrides(p):
    """What this profile changes, as label/value pairs. Empty means it changes nothing."""
    rows = nullable_overrides(p, _OVERRIDE_ROWS)
    # The padding columns are NOT NULL with a 0 default and inherit nothing (see
    # _global_defaults), so they have no unset state and cannot go through
    # nullable_overrides - "set" here is non-zero. Rendered as one pair because pre and
    # post are read as a pair, and placed first because that is the order the retired
    # table used.
    if p.pre_padding_minutes or p.post_padding_minutes:
        rows.insert(0, Override('Padding',
                                f'{p.pre_padding_minutes}m / {p.post_padding_minutes}m'))
    return rows


@profiles_bp.route('/profiles')
def profiles_list():
    profiles = RecordingProfile.query.order_by(RecordingProfile.name).all()
    rec_counts = dict(
        db.session.query(Recording.profile_id, db.func.count(Recording.id))
        # started_at IS NULL = never actually ran (SCHEDULED, or canceled before start).
        # Only recordings that started count as usage - same discriminator the recordings
        # list uses for rec-empty.
        .filter(Recording.profile_id.isnot(None), Recording.started_at.isnot(None))
        .group_by(Recording.profile_id)
        .all()
    )
    channel_counts = dict(
        db.session.query(Channel.default_profile_id, db.func.count(Channel.id))
        .filter(Channel.default_profile_id.isnot(None))
        .group_by(Channel.default_profile_id)
        .all()
    )
    cfg = load_config()
    defaults = _global_defaults(cfg)
    return render_template(
        'profiles.html',
        profiles=profiles,
        rec_counts=rec_counts,
        channel_counts=channel_counts,
        defaults=defaults,
        # Built here rather than in the template: the "did the user set this" test is
        # `is not None`, and a Jinja conditional written per field is where that quietly
        # becomes truthiness and starts hiding a profile's 0 (app/profile_forms.py).
        overrides={p.id: _overrides(p) for p in profiles},
        # The filename template is excluded here for the same reason it is excluded from
        # _OVERRIDE_ROWS, and is shown on its own line.
        default_summary=default_summary(defaults, _OVERRIDE_ROWS),
        profiles_json=[_profile_payload(p) for p in profiles],
    )


@profiles_bp.route('/api/profiles', methods=['POST'])
def create_profile():
    values, error = _read_profile_body()
    if error:
        return jsonify({'error': error}), 400

    @retry_on_locked()
    def _create_and_commit():
        profile = RecordingProfile(**values)
        db.session.add(profile)
        db.session.commit()
        return profile

    profile = _create_and_commit()
    return jsonify({'success': True, 'profile': _profile_payload(profile)})


@profiles_bp.route('/api/profiles/<int:profile_id>', methods=['PUT'])
def update_profile(profile_id):
    if db.session.get(RecordingProfile, profile_id) is None:
        return jsonify({'error': 'Profile not found'}), 404

    values, error = _read_profile_body()
    if error:
        return jsonify({'error': error}), 400

    @retry_on_locked()
    def _update_and_commit():
        # Re-fetched inside the closure, not captured outside it: a rolled-back retry
        # expires the instance's pending changes, so replaying setattr on a row fetched
        # before the retry would silently persist nothing (CLAUDE.md retry_on_locked).
        profile = db.session.get(RecordingProfile, profile_id)
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


@profiles_bp.route('/api/profiles/<int:profile_id>', methods=['DELETE'])
def delete_profile(profile_id):
    profile = db.session.get(RecordingProfile, profile_id)
    if profile is None:
        return jsonify({'error': 'Profile not found'}), 404
    name = profile.name

    # Two separate commits (unlink references, then delete the row) each need their
    # own retry_on_locked closure - see CLAUDE.md's db.session.commit() rule: a retry
    # that re-ran both commits from one decorator could re-delete-and-recreate state
    # incorrectly if the second commit failed after the first already succeeded.
    @retry_on_locked()
    def _unlink_references():
        Recording.query.filter_by(profile_id=profile_id).update({'profile_id': None})
        Channel.query.filter_by(default_profile_id=profile_id).update({'default_profile_id': None})
        db.session.commit()

    @retry_on_locked()
    def _delete_profile_row():
        p = db.session.get(RecordingProfile, profile_id)
        if p is not None:
            db.session.delete(p)
            db.session.commit()

    _unlink_references()
    _delete_profile_row()

    return jsonify({'success': True, 'name': name})
