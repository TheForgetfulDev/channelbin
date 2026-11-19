"""Live channel preview endpoints (app/preview.py).

Start and stop are POSTs on the channel; the playlist and segments are plain GETs the
player fetches on its own schedule. Every gate is enforced here, not in the page: the
account's connection limit is checked by start_preview() itself, and a segment name that
is not one ffmpeg wrote is a 404 rather than a path. The stream URL never appears in any
response - the browser only ever learns this app's own URLs (dev/changelog/1018).
"""
import logging
import os

from flask import Blueprint, jsonify, request, send_file, url_for

from .. import preview

log = logging.getLogger(__name__)

preview_bp = Blueprint('preview', __name__)

PLAYLIST_MIMETYPE = 'application/vnd.apple.mpegurl'
SEGMENT_MIMETYPE = 'video/mp2t'


def session_urls(session_id: str, round_no: int = None) -> dict:
    """A feed session's playlist names the round it is on (`round_no`): the player attaches to one
    round at a time, and a playlist fetch for a round that has ended is a 404."""
    playlist = (url_for('preview.preview_playlist', session_id=session_id, round=round_no)
                if round_no is not None else url_for('preview.preview_playlist', session_id=session_id))
    return {
        'status_url': url_for('preview.preview_status', session_id=session_id),
        'playlist_url': playlist,
        'stop_url': url_for('preview.preview_stop', session_id=session_id),
    }


def _urls(session) -> dict:
    return session_urls(session.id, session.round_no if session.source == preview.SOURCE_FEED else None)


@preview_bp.route('/api/channels/<int:channel_id>/preview', methods=['POST'])
def start_channel_preview(channel_id):
    body = request.get_json(silent=True) or {}
    try:
        session = preview.start_preview(channel_id, reencode=bool(body.get('reencode')))
    except preview.PreviewRefused as exc:
        return jsonify({'error': exc.message}), exc.status
    return jsonify({'success': True, **session.to_dict(), **_urls(session)})


@preview_bp.route('/api/feed/<int:feed_id>/preview', methods=['POST'])
def start_feed_preview(feed_id):
    """Preview one of ChannelBin's own channels on the IPTV output - a group, a sports channel, an
    Up next channel - as an IPTV player gets it (dev/changelog/1645). The feed id is checked by the
    play itself: one the output does not serve is a 404, one with nothing to play names why."""
    from .. import output_feed
    body = request.get_json(silent=True) or {}
    kind, _row_id = output_feed.decode_feed_id(feed_id)
    if kind == output_feed.KIND_CHANNEL:
        # A plain channel plays what its provider sends; its own preview is the one to use.
        return jsonify({'error': 'That is one of your IPTV provider\'s channels. Preview it from its '
                                 'channel page.'}), 400
    try:
        session = preview.start_feed_preview(feed_id, reencode=bool(body.get('reencode')))
    except preview.PreviewRefused as exc:
        return jsonify({'error': exc.message}), exc.status
    return jsonify({'success': True, **session.to_dict(), **_urls(session)})


@preview_bp.route('/api/preview/reencode')
def reencode_available():
    """Could a re-encode start right now, and if not, why not - asked by a player whose
    browser could not decode the copy, so it offers the re-encode or says why it cannot
    (dev/changelog/1461). The start routes ask again: the answer can change in between."""
    from ..config import load_config
    device, why = preview.reencode_device(load_config())
    return jsonify({'success': True, 'available': bool(device), 'reason': why})


@preview_bp.route('/api/channels/<int:channel_id>/screenshot', methods=['POST'])
def capture_channel_screenshot(channel_id):
    """The channel page's "Capture screenshot": one live frame, without a test. Blocks for
    the few seconds a capture takes. Records only when it was taken - never a ChannelTest,
    never a score observation (dev/changelog/1160)."""
    from datetime import datetime

    from .. import db
    from ..config import load_config, resolve_ffmpeg_path
    from ..database import Channel
    from ..db_utils import retry_on_locked
    from ..storage_dirs import manual_screenshot_path

    if db.session.get(Channel, channel_id) is None:
        return jsonify({'error': 'Channel not found'}), 404
    cfg = load_config()
    path = manual_screenshot_path(cfg, channel_id)
    try:
        source = preview.capture_frame(channel_id, path,
                                       resolve_ffmpeg_path(cfg['ffmpeg']['path']))
    except preview.PreviewRefused as exc:
        log.warning('Screenshot of channel %d not taken: %s', channel_id, exc.message)
        return jsonify({'error': exc.message}), exc.status

    @retry_on_locked()
    def _stamp_and_commit():
        channel = db.session.get(Channel, channel_id)
        if channel is None:
            return None
        channel.screenshot_captured_at = datetime.utcnow()
        db.session.commit()
        return channel.screenshot_captured_at

    captured_at = _stamp_and_commit()
    if captured_at is None:
        # Deleted while the frame was being taken: the file has no channel to belong to.
        try:
            os.unlink(path)
        except OSError:
            pass   # already gone
        return jsonify({'error': 'Channel not found'}), 404
    return jsonify({
        'success': True,
        'source': source,
        'url': url_for('channel_tests.serve_screenshot', filename=os.path.basename(path),
                       v=int(captured_at.timestamp())),
    })


@preview_bp.route('/api/preview/<session_id>/status')
def preview_status(session_id):
    session = preview.get_session(session_id)
    if session is None:
        return jsonify({'error': 'No such preview'}), 404
    return jsonify({'success': True, **session.to_dict(), **_urls(session)})


@preview_bp.route('/api/preview/<session_id>/stop', methods=['POST'])
def preview_stop(session_id):
    # Also the target of the page's `pagehide` beacon, which cannot set headers and so
    # carries the CSRF token as a form field instead - Flask-WTF reads either.
    stopped = preview.stop_preview(session_id, preview.REASON_USER)
    if not stopped and preview.get_session(session_id) is None:
        return jsonify({'error': 'No such preview'}), 404
    return jsonify({'success': True, 'stopped': stopped})


@preview_bp.route('/api/preview/<session_id>/index.m3u8')
def preview_playlist(session_id):
    path = preview.touch_playlist(session_id, request.args.get('round', type=int))
    if path is None:
        return jsonify({'error': 'No such preview, or it has stopped'}), 404
    resp = send_file(path, mimetype=PLAYLIST_MIMETYPE, conditional=False)
    # A live playlist changes every segment; a cached copy is a player stuck on the past.
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@preview_bp.route('/api/preview/<session_id>/<segment>')
def preview_segment(session_id, segment):
    path = preview.segment_path(session_id, segment)
    if path is None:
        return jsonify({'error': 'No such segment'}), 404
    resp = send_file(path, mimetype=SEGMENT_MIMETYPE, conditional=True)
    resp.headers['Cache-Control'] = 'no-store'
    return resp
