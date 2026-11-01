"""The output feed's endpoints: what an Xtream client asks a panel for, answered from the TV
Guide (app/output_feed.py, dev/docs/DESIGN-output-feed.md).

Gated per request on `integrations.output_feed.enabled` and the credential pair the client
sends on every call - in the query for the three `.php` endpoints, in the path for a stream.
"Disabled", "no credential set" and "wrong credential" all answer the same 401 body, the
precedent app/routes/ha.py sets; app/auth.py::SELF_GATED_BLUEPRINTS names this blueprint so
the interactive login gate does not also demand a browser login from an IPTV app. Registered
always, so turning the feed on needs no restart.

The answers are bare Xtream shapes, not the app's `{'success': True}` envelope, for the same
reason ha.py's are: the consumer is an IPTV app that reads the Xtream protocol, not this app's
own JavaScript. The measured client (TiviMate 5.3.3, dev/docs/evidence/) issues the bare auth
call and six catalog actions at add time, accepts `[]` for the VOD and series ones, never
calls get.php, and builds `/live/<user>/<pass>/<id>.ts` against the address it was given.
"""
import logging

from flask import Blueprint, Response, g, jsonify, redirect, request

from .. import output_feed
from ..config import load_config

log = logging.getLogger(__name__)

output_feed_bp = Blueprint('output_feed', __name__)

#: Live catalog actions, by what the feed answers them with.
_ACTION_CATEGORIES = 'get_live_categories'
_ACTION_STREAMS = 'get_live_streams'
#: Actions the measured client issues that this feed has nothing for - answered `[]`
#: (section 11 is where a VOD catalog would arrive later).
_EMPTY_ACTIONS = frozenset({'get_vod_categories', 'get_vod_streams', 'get_series_categories',
                            'get_series', 'get_vod_info', 'get_series_info',
                            'get_short_epg', 'get_simple_data_table'})


@output_feed_bp.before_request
def _require_credentials():
    cfg = load_config()
    fcfg = output_feed.feed_config(cfg)
    args = request.view_args or {}
    username = args.get('username', request.args.get('username'))
    password = args.get('password', request.args.get('password'))
    if not output_feed.credentials_ok(fcfg, username, password):
        # One body for every cause, so nothing outside can tell the feed is off from a
        # wrong password (section 6.1).
        return jsonify({'error': output_feed.AUTH_FAILED_MESSAGE}), 401
    g.cfg = cfg
    g.feed_cfg = fcfg
    return None


def _base_url() -> str:
    """The origin the request arrived on, which is the origin every served URL names.
    `flask.behind_proxy` wires ProxyFix, so this is the externally reachable one when the
    user has set it (section 3.5)."""
    return request.host_url.rstrip('/')


@output_feed_bp.route('/player_api.php')
def player_api():
    action = request.args.get('action') or ''
    if not action:
        host, _, port = request.host.partition(':')
        port = port or ('443' if request.scheme == 'https' else '80')
        return jsonify(output_feed.auth_payload(g.feed_cfg, host, port, request.scheme))
    if action == _ACTION_CATEGORIES:
        return jsonify(output_feed.category_payload(output_feed.catalog(g.cfg)))
    if action == _ACTION_STREAMS:
        return jsonify(output_feed.live_streams_payload(output_feed.catalog(g.cfg), _base_url()))
    if action in _EMPTY_ACTIONS:
        return jsonify([])
    log.info('Output feed: unknown player_api action %r from %s answered empty', action,
             request.remote_addr)
    return jsonify([])


@output_feed_bp.route('/get.php')
def get_php():
    text = output_feed.m3u_text(output_feed.catalog(g.cfg), _base_url(), g.feed_cfg)
    return Response(text, mimetype='audio/x-mpegurl',
                    headers={'Content-Disposition': 'attachment; filename="channelbin.m3u"'})


@output_feed_bp.route('/xmltv.php')
def xmltv():
    rows = output_feed.catalog(g.cfg)
    # Built whole inside the request: the generator reads the database, and a body
    # streamed after the request ends has no session to read it with.
    body = ''.join(output_feed.xmltv_lines(rows, g.cfg))
    return Response(body, mimetype='application/xml')


@output_feed_bp.route('/live/<username>/<password>/<int:feed_id>.ts')
@output_feed_bp.route('/live/<username>/<password>/<int:feed_id>')
def live(username, password, feed_id):
    """Play one feed id: a 302 to the member's provider URL in redirect mode, a relayed
    MPEG-TS body in proxy mode. Every refusal names its reason in the body."""
    client = request.remote_addr or ''
    try:
        if output_feed.mode_of(g.feed_cfg) == output_feed.MODE_REDIRECT:
            return redirect(output_feed.redirect_target(feed_id, client, g.cfg), code=302)
        session = output_feed.start_session(feed_id, client, g.cfg)
    except output_feed.FeedRefused as exc:
        return jsonify({'error': exc.message}), exc.status
    # The body runs after this request has ended, outside any request or app context, so
    # the stream holds no database session and no pool connection for as long as the IPTV app
    # watches. Everything it needs is on the session object.
    return Response(output_feed.stream(session), mimetype='video/mp2t',
                    headers={'Cache-Control': 'no-cache'}, direct_passthrough=True)
