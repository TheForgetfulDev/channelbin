"""Dev-only /mockups/ static-serving route (changelog 270).

Guards two things: the gate (with flask.serve_mockups off - the default - the blueprint is
not registered, so /mockups/ 404s), and that when on it lists and serves files from
dev/mockups/ while rejecting path traversal. MOCKUPS_DIR is repointed at a temp dir so the
test never depends on the gitignored dev/mockups/ contents.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
import app.routes.mockups as mockups_mod  # noqa: E402


class MockupsRouteTests(unittest.TestCase):
    def test_route_absent_when_flag_off(self):
        # Override explicitly off: relying on the ambient default would couple this test to
        # whatever the real config.yaml happens to say (serve_mockups may be enabled locally).
        t = make_test_app(extra_overrides={'flask': {'serve_mockups': False}})
        self.addCleanup(t.cleanup)
        self.assertEqual(t.app.test_client().get('/mockups/').status_code, 404)

    def test_serves_listing_and_files_when_flag_on(self):
        t = make_test_app(extra_overrides={'flask': {'serve_mockups': True}})
        self.addCleanup(t.cleanup)
        with open(os.path.join(t._tmpdir, 'x.html'), 'w') as fh:
            fh.write('<h1>hi</h1>')
        with mock.patch.object(mockups_mod, 'MOCKUPS_DIR', t._tmpdir):
            c = t.app.test_client()
            listing = c.get('/mockups/')
            self.assertEqual(listing.status_code, 200)
            self.assertIn('x.html', listing.get_data(as_text=True))
            self.assertEqual(c.get('/mockups/x.html').status_code, 200)
            # send_from_directory rejects traversal; missing file 404s.
            self.assertEqual(c.get('/mockups/nope.html').status_code, 404)
            self.assertIn(c.get('/mockups/../config.yaml').status_code, (400, 404))

    def test_primary_mockups_get_own_section_with_description_and_date(self):
        # A numbered mockup and its build support files land in the same folder - the top
        # section must show only the former, described from its own <title>, and the
        # support files must still appear in the full listing further down.
        t = make_test_app(extra_overrides={'flask': {'serve_mockups': True}})
        self.addCleanup(t.cleanup)
        with open(os.path.join(t._tmpdir, '07-widget-page.html'), 'w') as fh:
            fh.write('<html><head><title>Mockup 07 - Widget page (round 2)</title></head>'
                      '<body></body></html>')
        for support in ('07-body.part.html', '07-fields.json', '07-page.css', 'build07.py'):
            with open(os.path.join(t._tmpdir, support), 'w') as fh:
                fh.write('x')
        with mock.patch.object(mockups_mod, 'MOCKUPS_DIR', t._tmpdir):
            body = t.app.test_client().get('/mockups/').get_data(as_text=True)
        primary_section, _, rest = body.partition('<h2>All files</h2>')
        self.assertIn('07-widget-page.html', primary_section)
        self.assertIn('Widget page (round 2)', primary_section)
        self.assertRegex(primary_section, r'\d{4}-\d{2}-\d{2}')
        for support in ('07-body.part.html', '07-fields.json', '07-page.css', 'build07.py'):
            self.assertNotIn(support, primary_section)
            self.assertIn(support, rest)


if __name__ == '__main__':
    unittest.main()
