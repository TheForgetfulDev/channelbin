"""The Settings page against the design standard it was converted to.

DESIGN.md 15 (Settings, approved 2026-08-02) and the app-wide rules it inherits.
Rollout: dev/changelog/439. The Playwright pass that produced the approved design
also logged two conformance gaps in the shipped page; the search one is guarded
here (BUGS.md 2026-08-02 @ 08:53:41 PM ET). The other - no h1 on the Notifications
surface - is fixed in that rollout's part 2 and gets its case then.

The scrollspy's reading line and the rail's stickiness need real layout, so they
are Tier 4 (Playwright) rather than anything this file can assert.
"""
import json
import re
import unittest

from tests.support.app import make_test_app
from tests.support.config_sandbox import ConfigSandbox


class SettingsPageConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One app and one render for the whole class: every case reads the markup and
        # none rewrites the state it was rendered from (dev/changelog/979).
        cls.t = make_test_app()
        cls.client = cls.t.app.test_client()
        cls.html = cls.client.get('/settings').get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def test_page_has_exactly_one_h1(self):
        """DESIGN.md 3.10: a page announces itself once, with an h1.

        Characterization, not a regression guard - this one already passed against
        the pre-conversion page (the other six in this file did not). It is here so
        the Notifications surface's missing h1 has a sibling case to be added beside
        once that is fixed.
        """
        self.assertEqual(len(re.findall(r'<h1[ >]', self.html)), 1)

    def test_the_settings_search_has_a_clear_control(self):
        """DESIGN.md 3.11: a search field on a filter bar carries a clear x.

        The page shipped without one for months - the only search in the app that
        could not be emptied without selecting its text.
        """
        wrap = re.search(r'<div class="search-wrap"[^>]*>(.*?)</div>', self.html, re.S)
        self.assertIsNotNone(wrap, 'the settings search is not in a .search-wrap')
        self.assertIn('class="search-clear"', wrap.group(1))
        self.assertIn('id="ssearch"', wrap.group(1))

    def test_every_field_row_shows_its_config_key(self):
        """DESIGN.md 15.1: the config key is always visible, never hover-only.

        A phone has no hover, so a hover-only key is unreachable there. Counted
        rather than spot-checked: one macro emits every row, so a regression would
        drop all of them at once.
        """
        rows = len(re.findall(r'<div class="frow"[^>]*data-path=', self.html))
        keys = self.html.count('class="fl-key"')
        self.assertGreater(rows, 50, 'the settings page lost most of its fields')
        self.assertEqual(rows, keys)

    def test_the_maintenance_panels_are_gone_rather_than_copied(self):
        """DESIGN.md 7/16: the four panels moved to /maintenance (changelog 444).

        A relocation that leaves the originals behind is two sources of truth for
        one panel - two Restart buttons, two rebuild triggers, and a diff modal
        wired to whichever page loaded last. The ids are what the old page-local
        scroll targets pointed at, so their absence is the check that the move
        completed rather than duplicated.
        """
        for panel_id in ('m-backup', 'm-service', 'm-index', 'm-storage'):
            self.assertNotIn(f'id="{panel_id}"', self.html)
        self.assertNotIn('id="maint"', self.html)
        self.assertNotIn('search-index-rows', self.html)
        self.assertNotIn('storage-details-content', self.html)

    def test_settings_search_still_answers_for_the_relocated_panels(self):
        """The panels left the page; the questions they answer did not.

        Someone typing "backup" or "disk" into Settings is asking something this
        app can answer, so the four registry entries stay - as Pages carrying an
        href, since a scroll target that is no longer on the page would scroll
        nowhere and say nothing about why.
        """
        boot = re.search(r'<script type="application/json" id="settings-boot">(.*?)</script>',
                         self.html, re.S).group(1)
        surfaces = json.loads(boot)['surfaces']
        for surface_id in ('backup', 'service', 'index', 'storage'):
            entry = next(s for s in surfaces if s['id'] == surface_id)
            self.assertEqual(entry['kind'], 'Page')
            self.assertEqual(entry.get('href'), '/maintenance')
            self.assertNotIn('scroll', entry)

    def test_the_rail_and_the_sections_come_from_one_list(self):
        """DESIGN.md 15.7: the rail cannot disagree with the cards about what exists.

        The rail is built client-side by reading .sec-card, so the server renders
        the sections and an empty rail container - never a second hand-written list
        of section names that could drift from the cards.
        """
        cards = re.findall(r'class="card sec-card" id="sec-([a-z-]+)" data-sec="([a-z-]+)"', self.html)
        self.assertEqual(len(cards), 13)
        for elem_id, data_sec in cards:
            self.assertEqual(elem_id, data_sec)
        self.assertIn('<div id="rail"></div>', self.html)


class SecurityCardTests(unittest.TestCase):
    """The password gate's Settings card (dev/changelog/485)."""

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.html = self.client.get('/settings').get_data(as_text=True)

    def tearDown(self):
        self.t.cleanup()

    def test_security_card_exists(self):
        self.assertIn('id="sec-security"', self.html)

    def test_enable_toggle_is_disabled_when_no_password_is_set(self):
        m = re.search(r'<input type="checkbox" data-setting-path="auth\.enabled"[^>]*>', self.html)
        self.assertIsNotNone(m)
        self.assertIn('disabled', m.group(0))

    def test_password_sub_form_is_present(self):
        self.assertIn('id="auth-password-form"', self.html)
        self.assertIn('id="auth-new-password"', self.html)
        self.assertIn('id="auth-confirm-password"', self.html)
        # No password set yet in this test's default config, so no current-password
        # field - a first-time "Set password", not a "Change password".
        self.assertNotIn('id="auth-current-password"', self.html)
        self.assertIn('Set password', self.html)


class IntegrationsCardTests(ConfigSandbox):
    """The Home Assistant integration's Settings card.

    ConfigSandbox, not a bare TestCase: /settings renders from a runtime load_config(),
    which make_test_app() does not sandbox - so these assertions about the no-key-yet state
    read whatever the developer's own config.yaml had, and failed outright once the HA
    integration was enabled for real on this machine (dev/changelog/515)."""

    def setUp(self):
        super().setUp()
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.html = self.client.get('/settings').get_data(as_text=True)

    def tearDown(self):
        self.t.cleanup()

    def test_integrations_card_exists(self):
        self.assertIn('id="sec-integrations"', self.html)

    def test_enable_toggle_is_disabled_when_no_key_is_set(self):
        m = re.search(
            r'<input type="checkbox" data-setting-path="integrations\.home_assistant\.enabled"[^>]*>',
            self.html)
        self.assertIsNotNone(m)
        self.assertIn('disabled', m.group(0))

    def test_generate_key_button_is_present(self):
        self.assertIn('id="ha-generate-key-btn"', self.html)
        self.assertIn('Generate key', self.html)


class MobileStickyOffsetTests(unittest.TestCase):
    """The two coupled sticky offsets are one value read from one place.

    DESIGN.md 15.5 item 1 and CLAUDE.md's CSS rule. The mobile section picker
    sticks directly under the top bar, so its `top` and the bar's own height are
    the same number - and the mockup round measured what happens when they are
    written twice: 2px of drift made the picker name the section you just left
    after every jump. The bar's height is the --topnav-h token and the picker
    reads it, so neither can be edited alone.
    """

    def setUp(self):
        with open('static/css/style.css') as fh:
            self.style = fh.read()
        with open('templates/settings.html') as fh:
            self.settings = fh.read()

    def test_topnav_height_is_a_token(self):
        self.assertIn('--topnav-h:', self.style)
        self.assertRegex(self.style, r'\.topnav\s*\{[^}]*height:\s*var\(--topnav-h\)')

    def test_the_picker_reads_the_token_rather_than_restating_the_height(self):
        picker = re.search(r'\.secpick\s*\{[^}]*display:\s*block;[^}]*\}', self.settings, re.S)
        self.assertIsNotNone(picker, 'the mobile section picker rule is gone')
        self.assertIn('top: var(--topnav-h)', picker.group(0))

    def test_the_desktop_offset_exists_at_all(self):
        """The spy derives its reading line from scroll-margin-top, so a width with
        no scroll-margin gets a ~2px line and only calls a section current once its
        heading has scrolled off the top.

        Measured in the browser pass: the rail named the previous section for the
        first 50-100px of every section, because the desktop rule was missing and
        only the <=900px one existed.
        """
        desktop = self.settings.split('@media')[0]
        self.assertRegex(desktop, r'\.sec-card\s*\{\s*scroll-margin-top:\s*\d+px')

    def test_jump_targets_clear_both_sticky_layers(self):
        """A section jumped to must clear the top bar AND the picker, or it lands
        underneath them and the page looks like it scrolled to the wrong place."""
        self.assertRegex(
            self.settings,
            r'scroll-margin-top:\s*calc\(var\(--topnav-h\)\s*\+\s*var\(--pick-h\)')


if __name__ == '__main__':
    unittest.main()
