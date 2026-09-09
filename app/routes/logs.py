"""
Logs viewer page and API.

GET  /logs                    - HTML page
GET  /api/logs/history?tail=N - last N parsed log lines as JSON (default 1000)
GET  /api/logs/stream         - SSE stream of new log lines (file-tail approach)
"""
import json
import os
import re
import time

from flask import Blueprint, render_template, Response, stream_with_context, jsonify, request

from ..config import load_config

logs_bp = Blueprint('logs', __name__)

LOG_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \[(\w+)\] ([^:]+): (.*)$'
)
ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def _log_file_path():
    return load_config().get('logging', {}).get('file')


def _strip_ansi(text):
    return ANSI_RE.sub('', text)


def _parse_line(line):
    m = LOG_RE.match(line.rstrip())
    if not m:
        return None
    return {
        'ts': m.group(1),
        'level': m.group(2),
        'source': m.group(3).strip(),
        'message': _strip_ansi(m.group(4)),
    }


def _tail_lines(path, n):
    """Return last n lines of a file without reading the whole thing."""
    with open(path, 'rb') as f:
        f.seek(0, 2)
        size = f.tell()
        if size == 0:
            return []
        buf = b''
        pos = size
        while pos > 0 and buf.count(b'\n') < n + 1:
            chunk = min(65536, pos)
            pos -= chunk
            f.seek(pos)
            buf = f.read(chunk) + buf
    lines = buf.decode('utf-8', errors='replace').splitlines()
    return lines[-n:] if len(lines) > n else lines


def _parse_lines(lines):
    """Parse a list of raw log lines, accumulating multi-line entries."""
    records = []
    pending = None
    for line in lines:
        r = _parse_line(line)
        if r:
            if pending:
                records.append(pending)
            pending = r
        elif pending and line.strip():
            pending['message'] += '\n' + _strip_ansi(line)
    if pending:
        records.append(pending)
    return records


@logs_bp.route('/logs')
def logs_page():
    # The page says which file it is tailing. Without it a reader who sees "No log
    # history available." has no way to tell an empty log from a misconfigured path,
    # which is the silence principle 1 exists to forbid.
    return render_template('logs.html', log_path=_log_file_path())


@logs_bp.route('/api/logs/history')
def logs_history():
    path = _log_file_path()
    if not path or not os.path.isfile(path):
        return jsonify([])
    tail = min(int(request.args.get('tail', 1000)), 5000)
    lines = _tail_lines(path, tail)
    return jsonify(_parse_lines(lines))


@logs_bp.route('/api/logs/stream')
def logs_stream():
    path = _log_file_path()

    def generate():
        if not path or not os.path.isfile(path):
            yield ': no log file configured\n\n'
            return
        try:
            with open(path, 'r', errors='replace') as f:
                f.seek(0, 2)
                last_keepalive = time.time()
                while True:
                    line = f.readline()
                    if line:
                        r = _parse_line(line)
                        if r:
                            yield f'data: {json.dumps(r)}\n\n'
                    else:
                        now = time.time()
                        if now - last_keepalive > 15:
                            yield ': keepalive\n\n'
                            last_keepalive = now
                        time.sleep(0.3)
        except GeneratorExit:
            pass

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )
