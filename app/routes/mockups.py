"""Dev-only static serving of dev/mockups/.

Gated by flask.serve_mockups (default False) - this blueprint is only registered when
that flag is on (see app/__init__.py), so a clean/proxied deploy never exposes it. The
folder is tracked in git (not gitignored - dev/changelog/449) and holds standalone UI
mockups iterated on during design work; serving it here means reviewing them in the browser
instead of downloading each file. GET-only, read-only:
no CSRF surface, no DB writes.
"""
import os
import re
from datetime import datetime

from flask import Blueprint, abort, render_template_string, send_from_directory

from ..config import resolve_app_path

mockups_bp = Blueprint('mockups', __name__)

# Anchored to the app root, never the process CWD (see resolve_app_path).
MOCKUPS_DIR = resolve_app_path('dev/mockups')

# A mockup's own viewable file is "<number>-<slug>.html" (DESIGN.md §11.5) - everything
# else alongside it (*.part.html, *.json, *.css, *.js, build*.py, mock*.js) is a support
# file for the build, not something a reader opens directly.
_PRIMARY_RE = re.compile(r'^(\d+)-.*\.html$')
_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.I | re.S)
# Every mockup's <title> already names its purpose (e.g. "Mockup 27 - Settings x3,
# desktop (round 2)") - strip the boilerplate half so the listing shows just the
# description, since the number and filename are already shown alongside it.
_DESC_STRIP_RES = (
    re.compile(r'^channelbin\s*-\s*', re.I),
    re.compile(r'^mockup\s*\d*\s*[-—]\s*', re.I),
)


def _is_primary_mockup(name):
    return bool(_PRIMARY_RE.match(name)) and not name.endswith('.part.html')


def _mockup_description(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            head = f.read(8192)
    except OSError:
        return ''
    m = _TITLE_RE.search(head)
    if not m:
        return ''
    text = ' '.join(m.group(1).split())
    for pattern in _DESC_STRIP_RES:
        text = pattern.sub('', text)
    return text


_LISTING = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mockups</title>
<style>
  body { font: 16px/1.5 system-ui, sans-serif; max-width: 720px; margin: 2rem auto;
         padding: 0 1rem; color: #e6e6e6; background: #16181d; }
  h1 { font-size: 1.25rem; }
  h2 { font-size: .95rem; color: #9aa0a6; margin-top: 2.5rem; text-transform: uppercase;
       letter-spacing: .04em; }
  ul { list-style: none; padding: 0; }
  li { margin: .35rem 0; }
  a { color: #6db3f2; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .empty { color: #9aa0a6; }
  .mockups { display: flex; flex-direction: column; gap: 0; }
  .mockup { border-bottom: 1px solid #262a31; padding: .6rem 0; }
  .mockup a { font-weight: 600; font-size: 1.02rem; }
  .mockup .desc { color: #c2c6cc; margin-top: .2rem; }
  .mockup .date { color: #767c84; font-size: .82rem; margin-top: .2rem; }
</style>
</head>
<body>
  <h1>dev/mockups/</h1>
  {% if mockups %}
  <ul class="mockups">
    {% for m in mockups %}
    <li class="mockup">
      <a href="{{ m.name }}">{{ m.name }}</a>
      {% if m.desc %}<div class="desc">{{ m.desc }}</div>{% endif %}
      <div class="date">{{ m.date }}</div>
    </li>
    {% endfor %}
  </ul>
  {% else %}
  <p class="empty">No mockups found.</p>
  {% endif %}

  <h2>All files</h2>
  {% if files %}
  <ul>
    {% for name in files %}<li><a href="{{ name }}">{{ name }}</a></li>{% endfor %}
  </ul>
  {% else %}
  <p class="empty">No files in dev/mockups/.</p>
  {% endif %}
</body>
</html>"""


@mockups_bp.route('/mockups/')
def index():
    if not os.path.isdir(MOCKUPS_DIR):
        abort(404)
    try:
        entries = [
            entry for entry in os.scandir(MOCKUPS_DIR)
            if entry.is_file(follow_symlinks=False)
        ]
    except OSError:
        abort(404)

    files = sorted(entry.name for entry in entries)

    mockups = []
    for entry in entries:
        if not _is_primary_mockup(entry.name):
            continue
        mockups.append({
            'name': entry.name,
            'num': int(_PRIMARY_RE.match(entry.name).group(1)),
            'desc': _mockup_description(entry.path),
            'date': datetime.fromtimestamp(entry.stat().st_mtime).strftime('%Y-%m-%d'),
        })
    mockups.sort(key=lambda m: m['num'])

    return render_template_string(_LISTING, mockups=mockups, files=files)


@mockups_bp.route('/mockups/<path:filename>')
def file(filename):
    # send_from_directory rejects path traversal, so the <path:> converter is safe and
    # still resolves the relative assets the mockups reference (logo png, mock*.js).
    return send_from_directory(MOCKUPS_DIR, filename)
