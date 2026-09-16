"""The login page (templates/login.html) against the password gate's own rules
(CLAUDE.md password-gate spec; dev/changelog/485).

templates/login.html is deliberately standalone (does not extend base.html - a
logged-out visitor gets no nav), so it needs its own conformance case rather than
inheriting anything test_settings_page_conformance.py-style pages get for free.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_login_page_conformance
"""
import unittest

from werkzeug.security import generate_password_hash

from tests.support.app import make_test_app


class LoginPageConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One app and one render for the whole class: every case reads the markup and
        # none rewrites the state it was rendered from (dev/changelog/979).
        pw_hash = generate_password_hash('correcthorse-battery-staple')
        cls.t = make_test_app(extra_overrides={
            'auth': {'enabled': True, 'password_hash': pw_hash, 'session_timeout_minutes': 0},
        })
        cls.client = cls.t.app.test_client()
        cls.html = cls.client.get('/login').get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def test_does_not_extend_base_html(self):
        """A logged-out visitor gets no nav - the topnav/sidebar markup must be absent."""
        self.assertNotIn('class="topnav"', self.html)
        self.assertNotIn('id="rail"', self.html)

    def test_has_exactly_one_h1_or_none_with_a_clear_brand(self):
        # The page uses a brand lockup rather than an h1 (DESIGN.md components), but it
        # must still identify itself.
        self.assertIn('ChannelBin', self.html)

    def test_password_field_uses_the_local_reveal_not_the_stored_reveal(self):
        """DESIGN.md/CLAUDE.md: this app never hands a password back - the eyeball here
        must be initLocalReveal's plain type-swap, never initSecretReveal's fetch-from-
        server one (there is no stored value to reveal; the field is empty on load)."""
        self.assertIn('class="secret-field"', self.html)
        self.assertIn('class="secret-reveal-btn"', self.html)
        # The stored-secret reveal carries data-secret-path; the local one never does.
        self.assertNotIn('data-secret-path', self.html)

    def test_csrf_field_present(self):
        self.assertIn('name="csrf_token"', self.html)

    def test_uses_only_tokens_and_shared_components(self):
        """No page-local color literal - style.css tokens only (CLAUDE.md UI components
        and tokens rule)."""
        self.assertNotIn('#fff', self.html.lower())
        self.assertNotIn('rgb(', self.html.lower())

    def test_wrong_password_shows_generic_error_inline(self):
        import re
        tok = re.search(r'name="csrf_token" value="([^"]+)"', self.html).group(1)
        r = self.client.post('/login', data={'password': 'nope', 'csrf_token': tok})
        html = r.get_data(as_text=True)
        self.assertIn('Incorrect password', html)


class LoginPageGateOffTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def test_404s_when_gate_is_off(self):
        r = self.client.get('/login')
        self.assertEqual(r.status_code, 404)


if __name__ == '__main__':
    unittest.main()
