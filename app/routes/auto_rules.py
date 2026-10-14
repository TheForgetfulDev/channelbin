"""Auto-record rules: the Rules list, a rule's own page and the JSON API behind them
(app/auto_rules.py, DESIGN-autoschedule.md §8, mockup 42, dev/changelog/1189).

What the two pages show is built in app/auto_rules_view.py; this module only routes. A rule
is created and its search changed on the Programs search (the `rule` action context,
DESIGN-channel-search.md §3), and edited, paused, checked and deleted from either page.
"""
import logging

from flask import Blueprint, abort, jsonify, render_template, request, url_for

from .. import auto_rules, auto_rules_view, db
from ..database import (
    AutoRule, RULE_MATCH_SCHEDULED, RULE_MATCH_SKIPPED_CAP, RULE_MATCH_SKIPPED_EXISTING,
    RULE_MATCH_SKIPPED_LIMIT, RULE_MATCH_SKIPPED_REPEAT, RULE_MATCH_USER_MODIFIED,
    RecordingProfile,
)

log = logging.getLogger(__name__)

auto_rules_bp = Blueprint('auto_rules', __name__)

#: How every surface names a ledger verdict (DESIGN-autoschedule.md §2), in RULE_MATCH_ACTIONS
#: order: (action, one, many), `{n}` the count. One table: the evaluation toasts
#: (static/js/auto-rule-modal.js) receive it through their page's config, so a verdict cannot
#: be worded two ways. All six are named - tests/test_rules_page_conformance.py holds it to
#: RULE_MATCH_ACTIONS.
VERDICT_PHRASES = (
    (RULE_MATCH_SCHEDULED, '{n} scheduled', '{n} scheduled'),
    (RULE_MATCH_SKIPPED_EXISTING, '{n} already had a recording', '{n} already had a recording'),
    (RULE_MATCH_SKIPPED_REPEAT, '{n} repeat skipped', '{n} repeats skipped'),
    (RULE_MATCH_SKIPPED_CAP, '{n} held back by the cap', '{n} held back by the cap'),
    (RULE_MATCH_SKIPPED_LIMIT, '{n} over the limit', '{n} over the limit'),
    (RULE_MATCH_USER_MODIFIED, '{n} left alone after you edited it',
     '{n} left alone after you edited them'),
)


def verdict_phrases() -> list:
    """VERDICT_PHRASES as the JSON the page scripts read. `pick` marks the verdict that made a
    recording, which an evaluation's answer counts in its own `scheduled` field rather than
    among its skips (auto_rules.RuleOutcome.payload)."""
    return [{'action': a, 'one': one, 'many': many, 'pick': a == RULE_MATCH_SCHEDULED}
            for a, one, many in VERDICT_PHRASES]


def _rule_or_404(rule_id):
    rule = db.session.get(AutoRule, rule_id)
    if rule is None:
        return None, (jsonify({'error': 'Rule not found.'}), 404)
    return rule, None


def _evaluate(rule_id, saved=True):
    """evaluate_now() for a rule, after a save, a resume or a Check now. The rule is saved
    whatever its check does, so a check that raises must not turn the answer into a 500 the
    dialog reads as "not saved" - inviting a second, identical rule. It answers what happened
    instead, and queues the check to run again."""
    try:
        return auto_rules.evaluate_now(rule_id)
    except Exception as exc:
        db.session.rollback()
        log.exception('Auto-record rule #%d: a check asked for by hand failed', rule_id)
        auto_rules.queue_evaluation(f'rule #{rule_id}: its check failed')
        return {'queued': False, 'rule_id': rule_id, 'scheduled': 0, 'skipped': {},
                'already_scheduled': 0, 'retracked': 0, 'problems': None, 'abandoned': False,
                'guide_reach': None,
                'error': f'{auto_rules.describe_error(exc)}. '
                         + ('The rule is saved, and the check runs again shortly.' if saved
                            else 'The check runs again shortly.')}


def _rule_payload(rule_id):
    found = auto_rules.rule_list([rule_id])
    return found[0] if found else None


def _profiles():
    return [{'id': p.id, 'name': p.name}
            for p in RecordingProfile.query.order_by(RecordingProfile.name)]


def _page_config():
    """What both pages' scripts need for the shared rule actions (auto-rule-modal.js)."""
    from ..config import load_config
    from .channels import airing_search_url
    from ..channel_search import RULE_CONTEXT_NEW
    return {
        'apiBase': '/api/auto-rules/',
        'rulesUrl': url_for('auto_rules.rules_page'),
        'searchBase': url_for('channels.channel_browser'),
        'newRuleUrl': airing_search_url(rule=RULE_CONTEXT_NEW),
        'profiles': _profiles(),
        'defaultCap': auto_rules.default_cap(load_config()),
        'phrases': verdict_phrases(),
    }


@auto_rules_bp.route('/rules')
def rules_page():
    return render_template('rules.html', page_config=_page_config(),
                           **auto_rules_view.list_context())


@auto_rules_bp.route('/rules/<int:rule_id>')
def rule_page(rule_id):
    """A rule's own page. `hist` and `shown` are History's filter chip and how many of its
    rows show, so the filter survives a reload and the card can be swapped for a fresh render
    (rule-detail.js)."""
    from .recordings import read_section_pref
    ctx = auto_rules_view.page_context(
        rule_id, hist=request.args.get('hist', 'all'),
        shown=request.args.get('shown', auto_rules_view.HISTORY_PAGE, type=int))
    if ctx is None:
        abort(404)
    return render_template(
        'rule_detail.html', page_config=_page_config(),
        sections=list(auto_rules_view.SECTIONS), section_names=auto_rules_view.SECTION_NAMES,
        section_pref=read_section_pref(auto_rules_view.SECTIONS_PREF_KEY),
        sections_pref_key=auto_rules_view.SECTIONS_PREF_KEY, **ctx)


@auto_rules_bp.route('/api/auto-rules', methods=['GET'])
def list_rules_json():
    return jsonify({'success': True, 'rules': auto_rules.rule_list()})


@auto_rules_bp.route('/api/auto-rules', methods=['POST'])
def create_rule_json():
    """Save a rule from a Programs search and evaluate it at once (§4.1). The answer says
    what that evaluation did, or what it is queued behind."""
    data = request.get_json(silent=True) or {}
    try:
        values = auto_rules.read_rule_values(data)
    except auto_rules.RuleError as exc:
        return jsonify({'error': str(exc)}), 400
    rule_id = auto_rules.create_rule(values)
    evaluation = _evaluate(rule_id)
    return jsonify({'success': True, 'rule': _rule_payload(rule_id), 'evaluation': evaluation,
                    'url': url_for('auto_rules.rule_page', rule_id=rule_id)})


@auto_rules_bp.route('/api/auto-rules/<int:rule_id>', methods=['PUT'])
def update_rule_json(rule_id):
    """Edit a rule (§8): its settings from the edit dialog, or its search from the Programs
    search's Save to rule. Recordings it already scheduled are kept; its skip verdicts for
    future showings are dropped and judged afresh by the evaluation this runs."""
    rule, err = _rule_or_404(rule_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        values = auto_rules.read_rule_values(data, existing=rule)
    except auto_rules.RuleError as exc:
        return jsonify({'error': str(exc)}), 400
    if not auto_rules.update_rule(rule_id, values):
        return jsonify({'error': 'Rule not found.'}), 404
    evaluation = (_evaluate(rule_id)
                  if db.session.get(AutoRule, rule_id).enabled else None)
    return jsonify({'success': True, 'rule': _rule_payload(rule_id), 'evaluation': evaluation})


@auto_rules_bp.route('/api/auto-rules/<int:rule_id>/pause', methods=['POST'])
def pause_rule_json(rule_id):
    _rule, err = _rule_or_404(rule_id)
    if err:
        return err
    if not auto_rules.set_rule_enabled(rule_id, False):
        return jsonify({'error': 'Rule not found.'}), 404
    return jsonify({'success': True, 'rule': _rule_payload(rule_id)})


@auto_rules_bp.route('/api/auto-rules/<int:rule_id>/resume', methods=['POST'])
def resume_rule_json(rule_id):
    """Resume a paused rule and evaluate it at once, as a save does (§8)."""
    _rule, err = _rule_or_404(rule_id)
    if err:
        return err
    if not auto_rules.set_rule_enabled(rule_id, True):
        return jsonify({'error': 'Rule not found.'}), 404
    evaluation = _evaluate(rule_id)
    return jsonify({'success': True, 'rule': _rule_payload(rule_id), 'evaluation': evaluation})


@auto_rules_bp.route('/api/auto-rules/<int:rule_id>/check', methods=['POST'])
def check_rule_json(rule_id):
    """Check one rule now rather than at the next sweep - the same evaluation a save runs
    (§4.1), under the same admission ticket. A paused rule is never checked: that is what
    pausing means, so the answer says to resume it instead."""
    rule, err = _rule_or_404(rule_id)
    if err:
        return err
    if not rule.enabled:
        return jsonify({'error': 'This rule is paused, and a paused rule is not checked. '
                                 'Resume it to check it.'}), 409
    evaluation = _evaluate(rule_id, saved=False)
    return jsonify({'success': True, 'rule': _rule_payload(rule_id), 'evaluation': evaluation})


@auto_rules_bp.route('/api/auto-rules/<int:rule_id>', methods=['DELETE'])
def delete_rule_json(rule_id):
    """Delete a rule (§8). `cancel_scheduled` decides whether the SCHEDULED recordings it
    made are cancelled with it or kept; nothing that started is ever touched."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get('cancel_scheduled'), bool):
        return jsonify({'error': 'Say whether to cancel the rule\'s scheduled recordings '
                                 '(cancel_scheduled: true or false).'}), 400
    result = auto_rules.delete_rule(rule_id, data['cancel_scheduled'])
    if result is None:
        return jsonify({'error': 'Rule not found.'}), 404
    cancelled, kept = result
    return jsonify({'success': True, 'cancelled': len(cancelled), 'kept': kept})
