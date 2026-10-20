"""The CSRF token a route test posts with, read the way the browser reads it.

Every page carries base.html's `<meta name="csrf-token">`, which util.js's fetch wrapper
sends as X-CSRFToken on mutating requests; a rendered form carries the same token as a
hidden `csrf_token` field. Four test files each wrote their own extractor for one of the
two before this had a home (dev/changelog/1246). A test that is not about the token at all
opts out of the check instead, with `make_test_app(csrf=False)`.
"""
import re

_META_RE = re.compile(r'name="csrf-token" content="([^"]+)"')
_FIELD_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_token(client, path='/settings'):
    """The page's meta-tag token, fetched with `client`; None if the page carries none."""
    return csrf_meta(client.get(path).get_data(as_text=True))


def csrf_meta(html):
    """The `<meta name="csrf-token">` token in `html`, or None."""
    m = _META_RE.search(html)
    return m.group(1) if m else None


def csrf_field(html):
    """The hidden `csrf_token` form field in `html`, or None."""
    m = _FIELD_RE.search(html)
    return m.group(1) if m else None
