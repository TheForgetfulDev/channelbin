"""tests/support/jinjacache.py must be invisible to everything except the clock.

jinjacache hands every test app the same Jinja BytecodeCache so a template is compiled
once per process instead of once per app (122s -> 104s on the full suite). Unlike
tests/support/routecache.py it patches nobody's internals - `Flask.jinja_options` and
`bytecode_cache` are both documented - so the question is not "does this dependency still
work the way we assume", it is the one thing a compiled-code cache can plausibly get
wrong: serving stale bytecode for a template whose source has changed. That would be
vicious in this repo, because a template-only edit would then be invisible to the very
tests meant to catch it.

Both halves are asserted here: an edited template must recompile, and a page rendered by
a cached app must be byte-identical to the same page rendered by an uncached one.

If this file goes red, the fix is to update (or delete) tests/support/jinjacache.py -
never to relax these assertions. The suite is correct without the optimization, just
~18s slower.
"""
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import jinjacache  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

# Server-rendered pages with no URL parameters. Every one must render 200 for the
# comparison to mean anything - the test asserts that too, so a route rename turns this
# into a failure rather than a silently vacuous pass.
_PAGES = ('/', '/guide', '/channels', '/accounts', '/settings', '/alerts')

# The per-render volatile content in these pages, none of it a cache artifact, all of it
# normalized so the comparison is about the bytecode cache and nothing else:
#   * Flask-WTF mints a fresh CSRF token per session, in base.html's meta tag and in every
#     {{ csrf_field() }}.
#   * `/` (the Live Dashboard, dev/changelog/563) embeds the request's own capture time as
#     epoch ms in its #dash-timeline JSON blob (app/routes/dashboard.py::_timeline_payload).
#   * `/`'s storage tile renders a live statvfs of the DVR directory, so ordinary disk
#     activity between the two renders moves it. Also caught this test out for real: a
#     benchmark database written and deleted in a scratch dir mid-run took it from 24.0 GB
#     to 23.9 GB and the failure claimed the bytecode cache had changed the markup
#     (dev/changelog/724). Frozen in _render_pages rather than normalized here - see there.
#   * `/`'s "Errors, last hour" tile counts lines in the log file that
#     app/routes/dashboard.py::_recent_error_counts reads. That path resolves through a
#     runtime load_config(), which make_test_app overrides cannot reach (CLAUDE.md
#     ##Testing), so it reads the REAL log - a live file that grows on its own between the
#     two renders. It caught this test out for real: under a sharded run the two renders sat
#     far enough apart that the tile moved 122 -> 123 and the test failed claiming the
#     bytecode cache had changed the markup (dev/changelog/583).
_CSRF = re.compile(r'(csrf[-_]token"[^>]*?(?:content|value)=")[^"]*"')
_DASH_NOW = re.compile(r'("now":\s*)\d+')
_LOG_LINES = re.compile(r'\d+ log lines')


def _normalize(html):
    html = _CSRF.sub(r'\1TOKEN"', html)
    html = _DASH_NOW.sub(r'\1NOW', html)
    return _LOG_LINES.sub('N log lines', html)


class JinjaCacheFidelityTests(unittest.TestCase):
    """A cached-template app and a cold-compile app must render the same bytes."""

    def setUp(self):
        self.was_installed = jinjacache.is_installed()

    def tearDown(self):
        if self.was_installed and not jinjacache.is_installed():
            jinjacache.install()

    def _render_pages(self, cached):
        if cached:
            jinjacache.install()
        else:
            jinjacache.uninstall()
        # Frozen rather than normalized: the storage tile's markup is a bare
        # `<div class="m-v">24.0 GB</div>` with nothing distinctive to match on, so a regex
        # broad enough to catch it would blind the comparison to real markup differences.
        # Patching the one statvfs (app/routes/system.py::_disk_bytes - dashboard.py
        # imports it inside the function, so the module attribute is what resolves) is
        # exact. Both renders must be frozen to the SAME numbers, which is why the patch
        # wraps the whole method rather than one app.
        with mock.patch('app.routes.system._disk_bytes',
                        return_value=(53_687_091_200, 21_474_836_480)):
            t = make_test_app()
            try:
                return {p: (t.client.get(p).status_code,
                            _normalize(t.client.get(p).get_data(as_text=True)))
                        for p in _PAGES}
            finally:
                t.cleanup()

    def test_cached_app_renders_the_same_pages_as_a_cold_compile(self):
        cached = self._render_pages(cached=True)
        cold = self._render_pages(cached=False)
        for page in _PAGES:
            self.assertEqual(200, cold[page][0],
                             f'{page} did not render 200 - the comparison would be vacuous')
            self.assertEqual(cold[page][0], cached[page][0],
                             f'{page}: status differs under the bytecode cache')
            self.assertEqual(cold[page][1], cached[page][1],
                             f'{page}: markup differs under the bytecode cache')

    def test_edited_template_source_is_not_served_from_stale_bytecode(self):
        """The cache key must include a checksum of the source, not just the name.

        This is what makes the optimization safe to leave installed while templates are
        being edited: a second Environment reading changed bytes on disk must compile
        them, not replay the bucket the first one stored under the same template name.
        """
        from jinja2 import Environment, FileSystemLoader

        jinjacache.install()
        from flask import Flask
        cache = Flask.jinja_options.get('bytecode_cache')
        self.assertIsNotNone(cache, 'jinjacache.install() did not attach a bytecode cache')

        with tempfile.TemporaryDirectory(prefix='dvr_test_tpl_') as d:
            path = os.path.join(d, 'page.html')
            with open(path, 'w') as f:
                f.write('<p>{{ value }} first</p>')
            first = Environment(loader=FileSystemLoader(d), bytecode_cache=cache,
                                auto_reload=False).get_template('page.html').render(value=1)
            self.assertEqual('<p>1 first</p>', first)

            with open(path, 'w') as f:
                f.write('<p>{{ value }} second</p>')
            second = Environment(loader=FileSystemLoader(d), bytecode_cache=cache,
                                 auto_reload=False).get_template('page.html').render(value=1)

        self.assertEqual('<p>1 second</p>', second,
                         'the edited template was served from stale cached bytecode')


if __name__ == '__main__':
    unittest.main()
