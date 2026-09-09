import re

from flask import Blueprint, render_template, request, jsonify
from sqlalchemy.orm import selectinload

from .. import db
from ..accounts import _TAG_TOKEN_RE
from ..config import load_config
from ..database import Tag, TagPattern, RecordingProfile
from ..db_utils import retry_on_locked
from ..ui_constants import PRESET_COLORS

tags_bp = Blueprint('tags', __name__)

# Matches what the color picker can actually produce - the presets are 6-digit hex and the
# custom box is a free-text field. Enforced here rather than only in the modal, because the
# client-side check is presentation and this column is what the guide paints from: a junk
# value lands in a `style="background: ..."` attribute on every matching program cell.
_HEX_RE = re.compile(r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$')

_DEFAULT_COLOR = '#58a6ff'


@tags_bp.route('/tags')
def tags_list():
    # selectinload, not the lazy relationship: the patterns cell renders per row, so the
    # default lazy='select' issues one query per tag (CLAUDE.md - no hidden I/O in per-row
    # loops). Guarded by tests/test_scaling_pages.py::test_tags_page.
    tags = (Tag.query
            .options(selectinload(Tag.patterns))
            .order_by(Tag.name)
            .all())
    usage = _tag_usage()
    return render_template(
        'tags.html',
        tags=tags,
        usage=usage,
        preset_colors=PRESET_COLORS,
        tags_json=[_tag_payload(t) for t in tags],
    )


# ── JSON API (the create/edit modal in static/js/tag-modal.js) ────────────────

@tags_bp.route('/api/tags', methods=['POST'])
def create_tag():
    values, error = _read_tag_body()
    if error:
        return jsonify({'error': error}), 400

    @retry_on_locked()
    def _create_and_commit():
        tag = Tag(name=values['name'], color=values['color'])
        for pattern in values['patterns']:
            tag.patterns.append(TagPattern(pattern=pattern))
        db.session.add(tag)
        db.session.commit()
        return tag

    tag = _create_and_commit()
    return jsonify({'success': True, 'tag': _tag_payload(tag)})


@tags_bp.route('/api/tags/<int:tag_id>', methods=['PUT'])
def update_tag(tag_id):
    if db.session.get(Tag, tag_id) is None:
        return jsonify({'error': 'Tag not found'}), 404

    values, error = _read_tag_body(existing_id=tag_id)
    if error:
        return jsonify({'error': error}), 400

    @retry_on_locked()
    def _update_and_commit():
        # Re-fetched inside the closure: a rolled-back retry expires the instance's pending
        # changes, so replaying the mutation on a row fetched outside would persist nothing
        # (CLAUDE.md retry_on_locked).
        tag = db.session.get(Tag, tag_id)
        if tag is None:
            return None
        tag.name = values['name']
        tag.color = values['color']
        # Replacing the collection relies on delete-orphan to reap the old rows; assigning
        # a fresh list is what the form path did too, so an edit that only reorders patterns
        # still renumbers their ids. Nothing keys off a TagPattern id.
        tag.patterns = [TagPattern(pattern=p) for p in values['patterns']]
        db.session.commit()
        return tag

    tag = _update_and_commit()
    if tag is None:
        return jsonify({'error': 'Tag not found'}), 404
    return jsonify({'success': True, 'tag': _tag_payload(tag)})


@tags_bp.route('/api/tags/<int:tag_id>', methods=['DELETE'])
def delete_tag(tag_id):
    tag = db.session.get(Tag, tag_id)
    if tag is None:
        return jsonify({'error': 'Tag not found'}), 404
    name = tag.name

    @retry_on_locked()
    def _delete_and_commit():
        row = db.session.get(Tag, tag_id)
        if row is not None:
            db.session.delete(row)
            db.session.commit()

    _delete_and_commit()
    from ..channel_search import evict_tag_channel_ids
    evict_tag_channel_ids(tag_id)
    return jsonify({'success': True, 'name': name})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tag_payload(tag) -> dict:
    return {
        'id': tag.id,
        'name': tag.name,
        'color': tag.color,
        'patterns': [p.pattern for p in tag.patterns],
    }


def _clean_patterns(raw_patterns) -> list[str]:
    seen = []
    for p in raw_patterns:
        p = str(p).strip()
        if p and p not in seen:
            seen.append(p)
    return seen


def _tag_usage() -> dict[str, list[str]]:
    """Where each tag NAME is spelled out in settings, keyed by name.

    A tag is referenced by name, never by id, so deleting one silently changes what a
    filename template renders - the `{tag:live}` token simply stops resolving and the
    cleanup pass stops stripping. That is precisely the invisible behavior change the app
    exists to surface (CLAUDE.md principle 1), so the delete confirm names it before it
    happens rather than leaving the user to notice a filename changed weeks later.

    One config read and one profiles query, both hoisted out of any per-row loop.
    """
    usage: dict[str, list[str]] = {}

    def note(name: str, where: str):
        entries = usage.setdefault(name, [])
        if where not in entries:
            entries.append(where)

    rec_cfg = load_config().get('recording', {})

    for name in _TAG_TOKEN_RE.findall(rec_cfg.get('filename_template', '') or ''):
        note(name, 'the global filename template')
    for name in rec_cfg.get('filename_tags_remove', []) or []:
        note(name, 'filename cleanup (remove)')
    for name in rec_cfg.get('filename_tags_replace', []) or []:
        note(name, 'filename cleanup (replace)')

    for profile_name, template in db.session.query(
            RecordingProfile.name, RecordingProfile.filename_template).all():
        for name in _TAG_TOKEN_RE.findall(template or ''):
            note(name, f'the "{profile_name}" recording profile')

    return usage


def _read_tag_body(existing_id: int | None = None):
    """(values, None) or (None, first error message). The modal runs the same rules for
    presentation; this is the half that actually protects the row."""
    body = request.get_json(silent=True) or {}

    name = str(body.get('name', '')).strip().lower()
    patterns = _clean_patterns(body.get('patterns') or [])
    color = str(body.get('color', '') or '').strip() or _DEFAULT_COLOR

    if not name:
        return None, 'Name is required.'
    # isalnum() is Unicode-aware on purpose: the markers these tags exist to catch are
    # stylized Unicode, and a tag may reasonably be named in a non-Latin script.
    if not name.replace('-', '').replace('_', '').isalnum():
        return None, ('Name may only contain letters, numbers, hyphens, and underscores '
                      '(it is used in filename template placeholders).')

    dupe = Tag.query.filter(db.func.lower(Tag.name) == name)
    if existing_id is not None:
        dupe = dupe.filter(Tag.id != existing_id)
    if dupe.first() is not None:
        return None, f'A tag named "{name}" already exists.'

    if not patterns:
        return None, 'At least one match pattern is required.'
    if not _HEX_RE.match(color):
        return None, 'Color must be a hex value like #58a6ff.'

    return {'name': name, 'patterns': patterns, 'color': color}, None
