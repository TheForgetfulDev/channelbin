"""The Get started page: the setup steps in order, the optional ones worth doing next, the
two kinds of search, and the hand-off to the Readiness check (dev/changelog/1280)."""
import logging

from flask import Blueprint, current_app, jsonify, render_template, url_for

from .. import examples, setup_steps
from ..accounts import sync_signature
from .channels import airing_search_url
from .dashboard import recording_signature

log = logging.getLogger(__name__)

setup_bp = Blueprint('setup', __name__)


@setup_bp.route('/get-started')
def get_started():
    # Signatures first, for the dashboard's reason: a change landing between them and the
    # steps re-renders once more rather than going unseen.
    rec_sig = recording_signature()
    sync_sig = sync_signature()
    try:
        setup = setup_steps.page()
    except Exception as exc:                              # noqa: BLE001 - logged and said on the page
        log.warning('get started: setup steps could not be read: %s', exc)
        setup = None
    return render_template(
        'get_started.html',
        setup=setup,
        rec_sig=rec_sig,
        sync_sig=sync_sig,
        search_channels_url=url_for('channels.channel_browser'),
        search_programs_url=airing_search_url(),
    )


@setup_bp.route('/api/examples', methods=['POST'])
def load_examples():
    """"Try it with example channels" (app/examples.py). Refused while a load runs or
    while examples are already loaded."""
    started, message = examples.start(current_app._get_current_object())
    if not started:
        return jsonify({'error': message}), 409
    return jsonify({'success': True, 'message': message})


@setup_bp.route('/api/examples/removal')
def examples_removal():
    """What Delete the examples would take out and keep, and what stops it right now - the
    confirm dialog's content, from the same plan the removal follows."""
    p = examples.plan()
    if p['nothing']:
        return jsonify({'error': 'There are no example channels to remove.'}), 404
    return jsonify({'success': True, 'lines': p['lines'], 'blockers': p['blockers']})


@setup_bp.route('/api/examples/remove', methods=['POST'])
def remove_examples():
    removed, message = examples.remove()
    if not removed:
        return jsonify({'error': message}), 409
    return jsonify({'success': True, 'message': message,
                    'next_url': url_for('setup.get_started')})


@setup_bp.route('/api/examples/keep', methods=['POST'])
def keep_examples():
    """Keep the examples: the example account and group become ordinary ones."""
    kept, message = examples.keep()
    if not kept:
        return jsonify({'error': message}), 409
    return jsonify({'success': True, 'message': message})
