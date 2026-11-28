"""The Replacements page: channels the last sync stopped listing, matched to the rows that
took their place (dev/docs/DESIGN-channel-successors.md section 9, dev/changelog/1789 for the
approved mockup). Every write goes through app/channel_successors.py, which re-derives each
pairing from database ids; nothing the page sends is taken as proof that two rows are one
stream."""
import logging

from flask import Blueprint, jsonify, render_template, request
from sqlalchemy.exc import SQLAlchemyError

from .. import channel_successors as successors
from ..config import load_config
from ..fmt_utils import counted

log = logging.getLogger(__name__)

replacements_bp = Blueprint('replacements', __name__)

#: The most pairs one accept request may carry. The bulk accept is a loop of one-commit units,
#: so this bounds the request's length, not a transaction; the page sends Accept all in one.
MAX_PAIRS = 5000


@replacements_bp.app_context_processor
def inject_replacement_count():
    """The menu's Replacements link and the Dashboard's line, drawn only while something is
    stranded: three small queries per rendered page (the sync bounds, the membership and
    guide-row channels, the links among them), whatever the channel count - about 5 ms on
    prod's 183,000 (channel_successors.stranded_ids). A page renders the count it was read
    at; the Replacements page moves it on each accept (replacements.js)."""
    from flask import has_request_context
    if not has_request_context() or request.endpoint in (None, 'static'):
        return {}
    try:
        return {'nav_replacements': successors.stranded_count()}
    except SQLAlchemyError:
        # The menu must never fail a page; a database that cannot answer a COUNT is already
        # saying so on the page that asked for it.
        log.warning('Replacements count unavailable for the menu', exc_info=True)
        return {'nav_replacements': 0}


@replacements_bp.route('/channels/replacements')
def review_page():
    return render_template('channels/replacements.html', payload=successors.review_payload(),
                           group_filter=request.args.get('group', type=int))


def _carry_from(body):
    carry = body.get('carry')
    if carry is None:
        return set(successors.CARRY_PIECES)
    if not isinstance(carry, list):
        raise ValueError('carry must be a list')
    unknown = [c for c in carry if c not in successors.CARRY_PIECES]
    if unknown:
        raise ValueError(f'Unknown carry piece {unknown[0]!r}')
    return set(carry)


def _pairs_from(body):
    raw = body.get('pairs')
    if not isinstance(raw, list) or not raw:
        raise ValueError('Nothing to replace')
    if len(raw) > MAX_PAIRS:
        raise ValueError(f'At most {MAX_PAIRS} channels per request')
    pairs = []
    for p in raw:
        if not isinstance(p, dict):
            raise ValueError('Each pair names old_id and new_id')
        try:
            pairs.append((int(p['old_id']), int(p['new_id']), bool(p.get('picked'))))
        except (KeyError, TypeError, ValueError):
            raise ValueError('Each pair names old_id and new_id') from None
    return pairs


@replacements_bp.route('/api/replacements/accept', methods=['POST'])
def accept_api():
    """Accept one or many replacements: each is its own retry_on_locked unit
    (channel_successors.accept), so a request cut short is simply sent again. A pair the server
    would not propose is refused unless it says it was picked by hand."""
    body = request.get_json(silent=True) or {}
    try:
        carry = _carry_from(body)
        pairs = _pairs_from(body)
    except ValueError as err:
        return jsonify({'error': str(err)}), 400
    surface = (successors.SURFACE_CHANNEL if body.get('surface') == successors.SURFACE_CHANNEL
               else successors.SURFACE_REVIEW)
    cfg = load_config()
    done, skipped = [], []
    for old_id, new_id, picked in pairs:
        try:
            result = successors.accept(old_id, new_id, carry, cfg=cfg, surface=surface,
                                       picked=picked)
        except successors.ReplaceRefused as err:
            skipped.append({'old_id': old_id, 'reason': str(err)})
            continue
        done.append({'old_id': old_id, 'new_id': new_id, 'summary': successors.summary(result),
                     'groups': len(result['groups']), 'link_id': result.get('link_id'),
                     'score_before': result.get('score_before'),
                     'score_after': result.get('score_after')})
    groups = sum(d['groups'] for d in done)
    if len(pairs) > 1:
        # The bulk accept's one summary line beside the per-channel and per-group events
        # (section 9, Activity).
        log.info('Replaced %s across %s; %d skipped', counted(len(done), 'channel'),
                 counted(groups, 'group membership'), len(skipped))
    if not done and len(pairs) == 1:
        return jsonify({'error': skipped[0]['reason']}), 409
    return jsonify({'success': True, 'replaced': done, 'skipped': skipped})


@replacements_bp.route('/api/replacements/<int:link_id>/health', methods=['POST'])
def health_api(link_id):
    """The new channel page's "Count its history" switch (section 6)."""
    from .. import db
    from ..database import ChannelReplacement
    if db.session.get(ChannelReplacement, link_id) is None:
        return jsonify({'error': 'That replacement no longer exists'}), 404
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get('on'), bool):
        return jsonify({'error': 'on must be true or false'}), 400
    moved = successors.set_health_carried(link_id, body['on'], cfg=load_config())
    out = {'success': True, 'changed': moved is not None}
    if moved is not None:
        out.update(score_before=moved[0], score_after=moved[1])
    return jsonify(out)
