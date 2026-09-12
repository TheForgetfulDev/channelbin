"""The channel hide rules API - the blanket GLOB rules over category and channel name.

Sources 1-3 of the four that stack into `Channel.hidden` (app/channel_hiding.py,
dev/docs/DESIGN-channel-hiding.md). The engine lives in `channel_hiding`; nothing here
decides what a rule means, and in particular no client is trusted to say which rules apply -
every surface reads `Channel.hidden`, which only `channel_hiding.recompute()` writes.

Every mutating route follows the same three beats: commit the rule, then materialize, then
report whether the materialize actually ran. Committing first is what makes a refusal
harmless - the rule is durable and a retry reaches the same answer - and reporting the
refusal is what stops a person watching nothing change with no explanation.
"""
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request

from .. import channel_hiding, db
from ..database import (Account, Channel, ChannelHideRule,
                        HIDE_TARGETS, HIDE_TARGET_CATEGORY_GLOB, HIDE_TARGET_CATEGORY_EXACT,
                        HIDE_TARGET_NAME_GLOB)
from ..db_utils import retry_on_locked

channel_hide_rules_bp = Blueprint('channel_hide_rules', __name__)

_BASE = '/api/channel-hide-rules'

#: Display label per `ChannelHideRule.target` - the UI vocabulary for the three rule
#: sources, kept beside `_rule_payload` since the page and the API share one row shape.
TARGET_LABELS = {
    HIDE_TARGET_CATEGORY_GLOB: 'Category (pattern)',
    HIDE_TARGET_CATEGORY_EXACT: 'Category (exact)',
    HIDE_TARGET_NAME_GLOB: 'Channel name (pattern)',
}


def _rule_payload(rule: ChannelHideRule) -> dict:
    # Naive-UTC ISO strings, as every other JSON payload in the app emits: the display
    # timezone is applied client-side by static/js/util.js, so nothing here reaches for
    # config once per row.
    return {
        'id': rule.id,
        'account_id': rule.account_id,
        'target': rule.target,
        'pattern': rule.pattern,
        'enabled': bool(rule.enabled),
        'match_count': rule.match_count,
        'deferred_count': rule.deferred_count,
        'counted_at': rule.counted_at.isoformat() if rule.counted_at else None,
        'created_at': rule.created_at.isoformat() if rule.created_at else None,
        'updated_at': rule.updated_at.isoformat() if rule.updated_at else None,
    }


def _parse_account_id(data, key='account_id'):
    """The scope. Absent or null means global - one nullable column, not two lists."""
    raw = data.get(key)
    if raw in (None, '', 'null'):
        return None
    try:
        account_id = int(raw)
    except (TypeError, ValueError):
        raise ValueError('account_id must be an account id or null for a global rule')
    if db.session.get(Account, account_id) is None:
        raise ValueError(f'Account {account_id} does not exist.')
    return account_id


def _duplicate_of(target, pattern, account_id, exclude_id=None):
    """The existing rule this one would duplicate, or None.

    Asked here rather than left to the two partial UNIQUE indexes, so the answer is a 409
    naming the rule instead of an IntegrityError - the indexes are the backstop that keeps
    the pair a safe dict key, not the user-facing check.
    """
    q = ChannelHideRule.query.filter_by(target=target, pattern=pattern)
    q = (q.filter(ChannelHideRule.account_id.is_(None)) if account_id is None
         else q.filter(ChannelHideRule.account_id == account_id))
    if exclude_id is not None:
        q = q.filter(ChannelHideRule.id != exclude_id)
    return q.first()


def _materialize_and_report(label: str) -> dict:
    """Re-apply the rules to the channel table, and say whether it happened.

    A refusal is routed, never swallowed: it raises an alert naming the blocker and queues
    one retry, so the saved-but-not-applied window is both visible and self-closing.
    """
    result = channel_hiding.materialize(label)
    if result.granted:
        return {'materialized': True, 'channels_recomputed': result.rows}
    from ..scheduler import defer_hide_materialize
    defer_hide_materialize(result.reason)
    return {'materialized': False, 'refusal': result.reason}


@channel_hide_rules_bp.route('/channels/hide-rules')
def hide_rules_page():
    """The rules UI - pattern lists with live match counts, and a browser over every
    provider category (dev/changelog/780).

    The summary tiles are real aggregates read here, not recomputed client-side: `hidden`/
    `hidden_deferred` are already the materialized answer (channel_hiding.recompute()), so
    a second client-side derivation would just be a second, driftable copy of the same sum.
    Every mutation the page makes reloads afterward, so these numbers are never stale for
    longer than one round trip.

    `pending_materialize` is the saved-but-not-applied state: a refused pass leaves a queued
    retry, and this page is where that has to be visible, since the rules look saved and the
    channel counts will not have moved (dev/changelog/928).
    """
    from ..scheduler import pending_hide_materialize

    rules = ChannelHideRule.query.order_by(ChannelHideRule.id).all()
    accounts = Account.query.order_by(Account.name).all()
    total_channels = Channel.query.count()
    hidden_count = Channel.query.filter_by(hidden=True).count()
    deferred_count = Channel.query.filter_by(hidden_deferred=True).count()
    enabled_rules = sum(1 for r in rules if r.enabled)
    return render_template(
        'channels/hide_rules.html',
        rules=rules,
        target_labels=TARGET_LABELS,
        rules_json=[_rule_payload(r) for r in rules],
        accounts=accounts,
        accounts_json=[{'id': a.id, 'name': a.name, 'channel_count': a.channel_count}
                       for a in accounts],
        total_channels=total_channels,
        hidden_count=hidden_count,
        deferred_count=deferred_count,
        enabled_rules=enabled_rules,
        pending_materialize=pending_hide_materialize(),
    )


@channel_hide_rules_bp.route(_BASE, methods=['GET'])
def list_rules():
    """Every rule, oldest first. Disabled ones included - a switched-off rule is part of the
    list a person is reading, and it still carries what it would hide."""
    q = ChannelHideRule.query
    if 'account_id' in request.args:
        try:
            account_id = _parse_account_id(request.args)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        q = (q.filter(ChannelHideRule.account_id.is_(None)) if account_id is None
             else q.filter(ChannelHideRule.account_id == account_id))
    target = request.args.get('target')
    if target:
        if target not in HIDE_TARGETS:
            return jsonify({'error': f'target must be one of {", ".join(HIDE_TARGETS)}'}), 400
        q = q.filter(ChannelHideRule.target == target)
    rules = q.order_by(ChannelHideRule.id).all()
    return jsonify({'success': True, 'rules': [_rule_payload(r) for r in rules],
                    'targets': list(HIDE_TARGETS)})


@channel_hide_rules_bp.route(f'{_BASE}/preview', methods=['POST'])
def preview_rule():
    """What a pattern would hide, without saving it.

    POST rather than GET because a pattern is arbitrary text that has no business being
    URL-encoded into a query string and then into a server log. Costs one scan of the
    channel table, so a caller typing into a field debounces rather than firing per
    keystroke - the same SQLite write lock the recorder needs is on the other side of it.
    """
    data = request.get_json(silent=True) or {}
    try:
        account_id = _parse_account_id(data)
        target = data.get('target')
        pattern = channel_hiding.validate_pattern(target, data.get('pattern'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    out = channel_hiding.preview(target, pattern, account_id)
    return jsonify({'success': True, **out})


@channel_hide_rules_bp.route(f'{_BASE}/categories', methods=['GET'])
def list_categories():
    """Every provider category with its channel count - what an exact pick is chosen from.

    Categories are identified by NAME, not by `category_id`: measured across four real
    accounts, `category_id` is populated on exactly one of them and empty on two of the three
    Xtream ones, so the name is the only identifier present everywhere.
    """
    try:
        account_id = _parse_account_id(request.args) if 'account_id' in request.args else None
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'success': True, 'categories': channel_hiding.category_list(account_id)})


@channel_hide_rules_bp.route(_BASE, methods=['POST'])
def create_rule():
    """Add a rule, then apply it.

    A rule that would hide its ENTIRE scope is refused unless the request confirms it. That
    is measured rather than sniffed from the pattern's punctuation: `[a-zA-Z0-9]*` is as
    total as `*` and no syntax check would say so.
    """
    data = request.get_json(silent=True) or {}
    try:
        account_id = _parse_account_id(data)
        target = data.get('target')
        pattern = channel_hiding.validate_pattern(target, data.get('pattern'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    existing = _duplicate_of(target, pattern, account_id)
    if existing is not None:
        return jsonify({'error': 'That rule already exists.',
                        'rule': _rule_payload(existing)}), 409

    checked = channel_hiding.preview(target, pattern, account_id, sample_limit=20)
    if checked['hides_everything'] and not data.get('confirm'):
        scope = 'every channel you have' if account_id is None else 'every channel on this account'
        return jsonify({
            'error': f'That pattern matches {scope} - all {checked["matched"]:,} of them. '
                     f'Send confirm to save it anyway.',
            'preview': checked}), 409

    enabled = bool(data.get('enabled', True))

    @retry_on_locked()
    def _create_and_commit():
        rule = ChannelHideRule(account_id=account_id, target=target, pattern=pattern,
                               enabled=enabled, created_at=datetime.utcnow())
        db.session.add(rule)
        db.session.commit()
        return rule.id

    rule_id = _create_and_commit()
    applied = _materialize_and_report(f'rule {rule_id} added')
    rule = db.session.get(ChannelHideRule, rule_id)
    return jsonify({'success': True, 'rule': _rule_payload(rule),
                    'preview': checked, **applied})


@channel_hide_rules_bp.route(f'{_BASE}/<int:rule_id>', methods=['PATCH'])
def update_rule(rule_id):
    """Edit a rule's pattern, scope or on/off switch, then re-apply."""
    rule = db.session.get(ChannelHideRule, rule_id)
    if rule is None:
        return jsonify({'error': 'Rule not found'}), 404
    data = request.get_json(silent=True) or {}

    target = data.get('target', rule.target)
    pattern = data.get('pattern', rule.pattern)
    try:
        pattern = channel_hiding.validate_pattern(target, pattern)
        account_id = _parse_account_id(data) if 'account_id' in data else rule.account_id
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    enabled = bool(data.get('enabled', rule.enabled))

    existing = _duplicate_of(target, pattern, account_id, exclude_id=rule_id)
    if existing is not None:
        return jsonify({'error': 'Another rule already says that.',
                        'rule': _rule_payload(existing)}), 409

    checked = channel_hiding.preview(target, pattern, account_id, sample_limit=20)
    if enabled and checked['hides_everything'] and not data.get('confirm'):
        scope = 'every channel you have' if account_id is None else 'every channel on this account'
        return jsonify({
            'error': f'That pattern matches {scope} - all {checked["matched"]:,} of them. '
                     f'Send confirm to save it anyway.',
            'preview': checked}), 409

    @retry_on_locked()
    def _update_and_commit():
        # Re-fetched inside the closure: a rolled-back session expires every pending
        # attribute change, so a retry that reused the row fetched above would commit an
        # empty transaction and report success.
        row = db.session.get(ChannelHideRule, rule_id)
        row.target, row.pattern, row.account_id = target, pattern, account_id
        row.enabled = enabled
        row.updated_at = datetime.utcnow()
        db.session.commit()

    _update_and_commit()
    applied = _materialize_and_report(f'rule {rule_id} edited')
    rule = db.session.get(ChannelHideRule, rule_id)
    return jsonify({'success': True, 'rule': _rule_payload(rule),
                    'preview': checked, **applied})


@channel_hide_rules_bp.route(f'{_BASE}/<int:rule_id>', methods=['DELETE'])
def delete_rule(rule_id):
    """Drop a rule and give back whatever it was hiding.

    Nothing to clean up beyond the row itself: a rule owns no derived state. `Channel.hidden`
    is recomputed from the rules that remain, so a channel this rule was the only reason for
    becomes visible again on its own - which is the same property that lets a format-mismatch
    member become eligible again without any re-enable machinery.
    """
    rule = db.session.get(ChannelHideRule, rule_id)
    if rule is None:
        return jsonify({'error': 'Rule not found'}), 404
    payload = _rule_payload(rule)

    @retry_on_locked()
    def _delete_and_commit():
        row = db.session.get(ChannelHideRule, rule_id)
        if row is not None:
            db.session.delete(row)
        db.session.commit()

    _delete_and_commit()
    applied = _materialize_and_report(f'rule {rule_id} deleted')
    return jsonify({'success': True, 'rule': payload, **applied})
