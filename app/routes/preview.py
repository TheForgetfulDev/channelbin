"""Live channel preview endpoints (app/preview.py).

Start and stop are POSTs on the channel; the playlist and segments are plain GETs the
player fetches on its own schedule. Every gate is enforced here, not in the page: the
account's connection limit is checked by start_preview() itself, and a segment name that
is not one ffmpeg wrote is a 404 rather than a path. The stream URL never appears in any
response - the browser only ever learns this app's own URLs (dev/changelog/1018).
"""
import logging

from flask import Blueprint, jsonify, send_file, url_for

from .. import preview

log = logging.getLogger(__name__)

preview_bp = Blueprint('preview', __name__)

PLAYLIST_MIMETYPE = 'application/vnd.apple.mpegurl'
SEGMENT_MIMETYPE = 'video/mp2t'


def _urls(session_id: str) -> dict:
    return {
        'status_url': url_for('preview.preview_status', session_id=session_id),
        'playlist_url': url_for('preview.preview_playlist', session_id=session_id),
        'stop_url': url_for('preview.preview_stop', session_id=session_id),
    }


@preview_bp.route('/api/channels/<int:channel_id>/preview', methods=['POST'])
def start_channel_preview(channel_id):
    try:
        session = preview.start_preview(channel_id)
    except preview.PreviewRefused as exc:
        return jsonify({'error': exc.message}), exc.status
    return jsonify({'success': True, **session.to_dict(), **_urls(session.id)})


@preview_bp.route('/api/preview/<session_id>/status')
def preview_status(session_id):
    session = preview.get_session(session_id)
    if session is None:
        return jsonify({'error': 'No such preview'}), 404
    return jsonify({'success': True, **session.to_dict(), **_urls(session.id)})


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
    path = preview.touch_playlist(session_id)
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
