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
import os
import re
import unittest
from html.parser import HTMLParser

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


class FullWidthTests(unittest.TestCase):
    """DESIGN.md 2: Settings fills the content width like every other page.

    dev/changelog/1002. The page-scoped 1400px caps on Settings and on the
    Notifications Delivery card were removed; the shared field row's column gap
    is what keeps a wide row's description text off its control.
    """

    def setUp(self):
        with open('static/css/style.css') as fh:
            self.style = fh.read()
        self.templates = {}
        for name in ('settings.html', 'notifications_settings.html'):
            with open(f'templates/{name}') as fh:
                self.templates[name] = fh.read()

    def test_neither_settings_surface_caps_its_own_width(self):
        for name, src in self.templates.items():
            with self.subTest(template=name):
                self.assertNotRegex(src, r'max-width:\s*1400px')

    def test_the_set_wrap_rule_carries_no_width_cap(self):
        rule = re.search(r'\.set-wrap\s*\{[^}]*\}', self.templates['settings.html'])
        self.assertIsNotNone(rule, 'the .set-wrap rule is gone')
        self.assertNotIn('max-width', rule.group(0))

    def test_field_row_keeps_text_40px_off_the_control(self):
        rule = re.search(r'\n\.frow\s*\{[^}]*\}', self.style)
        self.assertIsNotNone(rule, 'the shared .frow rule is gone')
        self.assertRegex(rule.group(0), r'gap:\s*18px\s+40px;')


class DefaultLineTests(unittest.TestCase):
    """Guards BUGS.md 2026-09-17 @ 05:56:42 AM "Settings Default: lines had drifted from the code".

    The `Default:` line under a setting is looked up from `_DEFAULTS` by the row's path
    (dev/changelog/1003). Asserted on the rendered pages, row by row, so a row that
    renders some other text - or a path that is not a real config key - fails here.
    """

    _ROW_RE = re.compile(r'<div class="frow[^"]*" data-path="([^"]+)"(.*?)<div class="fl-key">', re.S)
    _DEFAULT_RE = re.compile(r'<div class="fl-default">Default: <code>(.*?)</code></div>', re.S)

    @classmethod
    def setUpClass(cls):
        cls.t = make_test_app()
        client = cls.t.app.test_client()
        cls.pages = {url: client.get(url).get_data(as_text=True)
                     for url in ('/settings', '/settings/notifications')}

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def _rows(self, html):
        """{path: (default lines, is read-only)} for every field row on the page."""
        return {path: (self._DEFAULT_RE.findall(body), 'read-only - edit in config.yaml' in body)
                for path, body in self._ROW_RE.findall(html)}

    def _default_lines(self, html):
        return {path: lines for path, (lines, _) in self._rows(html).items()}

    def test_every_default_line_is_the_code_default(self):
        from markupsafe import escape
        from app.config import default_display
        for url, html in self.pages.items():
            rows = self._rows(html)
            self.assertTrue(rows, f'{url} rendered no field rows')
            for path, (lines, readonly) in rows.items():
                with self.subTest(url=url, path=path):
                    expected = [] if readonly else [str(escape(default_display(path)))]
                    self.assertEqual(lines, expected)

    def test_the_rows_that_had_drifted_show_the_real_default(self):
        rows = self._default_lines(self.pages['/settings'])
        self.assertEqual(rows['recording.post_script.enabled'], ['false'])
        self.assertEqual(rows['sync.sync_interval_hours'], ['12'])
        self.assertEqual(rows['channel_testing.test_duration_seconds'], ['30'])
        self.assertEqual(rows['channel_testing.wait_between_channels_seconds'], ['30'])
        self.assertEqual(rows['recording.filename_template'],
                         ['{date} - {title} - {sub_title} - {channel}'])

    def test_display_spells_values_the_way_config_yaml_does(self):
        from unittest import mock
        from app import config
        leaves = {'a.bool': False, 'a.none': None, 'a.empty': '', 'a.list': [],
                  'a.float': 1.0, 'a.frac': 1.5, 'a.str': 'poor', 'a.int': 0}
        with mock.patch.dict(config._DEFAULT_LEAVES, leaves):
            got = {k: config.default_display(k) for k in leaves}
        self.assertEqual(got, {'a.bool': 'false', 'a.none': '(none)', 'a.empty': '(empty)',
                               'a.list': '(empty)', 'a.float': '1', 'a.frac': '1.5',
                               'a.str': 'poor', 'a.int': '0'})
        self.assertEqual(config.default_display(''), '')
        with self.assertRaises(KeyError):
            config.default_display('no.such.key')


class _GroupParser(HTMLParser):
    """Reads the Settings page into {card id: [(group id, heading, [row paths])]}.

    Rows outside any group are listed under group id None, so a row that escapes its
    group is visible to the assertions rather than silently skipped.
    """

    def __init__(self):
        super().__init__()
        self.cards = {}
        self._stack = []   # (tag, kind) for every open section/div
        self._card = None
        self._group = None
        self._in_heading = False

    def handle_starttag(self, tag, attrs):
        if tag not in ('section', 'div', 'strong'):
            return
        a = dict(attrs)
        if tag == 'strong':
            if self._stack and self._stack[-1][1] == 'heading':
                self._in_heading = True
            return
        kind = None
        if tag == 'section' and 'data-sec' in a:
            kind = 'card'
            self._card = a['data-sec']
            self.cards[self._card] = []
        elif 'data-sub' in a and self._card:
            kind = 'group'
            self._group = [a['data-sub'], '', []]
            self.cards[self._card].append(self._group)
        elif 'sec-sub' in (a.get('class') or '').split() and self._group:
            kind = 'heading'
        elif a.get('data-path') and self._card:
            groups = self.cards[self._card]
            if self._group is None:
                if not groups or groups[-1][0] is not None:
                    groups.append([None, '', []])
                groups[-1][2].append(a['data-path'])
            else:
                self._group[2].append(a['data-path'])
        self._stack.append((tag, kind))

    def handle_endtag(self, tag):
        if tag == 'strong':
            self._in_heading = False
            return
        if tag not in ('section', 'div') or not self._stack:
            return
        _, kind = self._stack.pop()
        if kind == 'group':
            self._group = None
        elif kind == 'card':
            self._card = None

    def handle_data(self, data):
        if self._in_heading and self._group is not None:
            self._group[1] += data.strip()


class SubGroupTests(unittest.TestCase):
    """DESIGN.md 15: each card is ordered into groups of fields that share a reader,
    each group a `[data-sub]` wrapper with its own heading, and a field that only
    matters while a switch is on sits directly under that switch (dev/changelog/1004).
    """

    GROUPS = {
        'recording': ['Output', 'Retention', 'Conversion', 'Conversion resilience',
                      'Collision avoidance', 'After completion',
                      'Measurement and local contention', 'Thumbnails and logos'],
        'watchdog': ['Stall detection', 'Moving off a member', 'Dead-stream detection'],
        'ffmpeg': ['Binaries', 'Capture', 'Join'],
        'sync': ['Connections', 'Sync schedule', 'Guide data', 'Stream URLs',
                 'Channel lifecycle', 'Search'],
        'channel-testing': ['Scheduling around recordings', 'Test execution', 'Scoring',
                            'Screenshots and history', 'Pre-recording checks',
                            'Maintenance window'],
    }

    # (gate, field): the field's row comes immediately after the gate's row, in the
    # same group. Only gates with no override elsewhere in the app are listed - the
    # post_process.enabled gate spans three groups and is checked by order below.
    DIRECTLY_UNDER = [
        ('recording.post_process.reencode_mode', 'recording.post_process.video_crf'),
        ('recording.post_process.auto_restart', 'recording.post_process.max_restart_attempts'),
        ('recording.post_process.collision_policy',
         'recording.post_process.collision_lookahead_multiplier'),
        ('recording.move_on_complete.enabled', 'recording.move_on_complete.destination'),
        ('recording.post_script.enabled', 'recording.post_script.path'),
        ('recording.live_thumbnail.enabled', 'recording.live_thumbnail.auto_refresh_seconds'),
        ('auth.enabled', 'auth.session_timeout_minutes'),
        ('config_backup.enabled', 'config_backup.backup_hour_et'),
        ('debug.xtream_debug_mode', 'debug.xtream_dump_dir'),
    ]

    POST_PROCESS_GATED = [
        'recording.post_process.format', 'recording.post_process.delete_source',
        'recording.post_process.pre_output_timeout_seconds',
        'recording.post_process.reencode_mode', 'recording.post_process.audio_bitrate_kbps',
        'recording.post_process.auto_restart', 'recording.post_process.stall_seconds',
        'recording.post_process.progress_interval_seconds',
        'recording.post_process.collision_policy',
    ]

    @classmethod
    def setUpClass(cls):
        cls.t = make_test_app()
        parser = _GroupParser()
        parser.feed(cls.t.app.test_client().get('/settings').get_data(as_text=True))
        cls.cards = parser.cards

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def _flat_rows(self):
        return [(card, gid, path) for card, groups in self.cards.items()
                for gid, _, paths in groups for path in paths]

    def test_each_grouped_card_has_its_groups_in_order(self):
        for card, headings in self.GROUPS.items():
            with self.subTest(card=card):
                self.assertEqual([h for _, h, _ in self.cards[card]], headings)

    def test_every_row_in_a_grouped_card_is_inside_a_group(self):
        for card in self.GROUPS:
            with self.subTest(card=card):
                loose = [p for gid, _, paths in self.cards[card] if gid is None for p in paths]
                self.assertEqual(loose, [])
                self.assertTrue(all(paths for _, _, paths in self.cards[card]),
                                'a group with no rows')

    def test_group_ids_are_unique_on_the_page(self):
        ids = [gid for groups in self.cards.values() for gid, _, _ in groups if gid]
        self.assertEqual(len(ids), len(set(ids)))

    def test_gated_fields_sit_directly_under_their_gate(self):
        rows = self._flat_rows()
        where = {path: i for i, (_, _, path) in enumerate(rows)}
        for gate, field in self.DIRECTLY_UNDER:
            with self.subTest(gate=gate, field=field):
                self.assertEqual(where[field], where[gate] + 1)
                self.assertEqual(rows[where[field]][1], rows[where[gate]][1])

    def test_post_process_gated_fields_follow_the_switch(self):
        rows = self._flat_rows()
        where = {path: i for i, (_, _, path) in enumerate(rows)}
        gate = where['recording.post_process.enabled']
        for field in self.POST_PROCESS_GATED:
            with self.subTest(field=field):
                self.assertGreater(where[field], gate)
                self.assertEqual(rows[where[field]][0], 'recording')

    def test_no_output_timeout_is_a_resilience_setting(self):
        """It is read by the same supervised runner as auto-restart and the stall timeout."""
        groups = {gid: paths for gid, _, paths in self.cards['recording']}
        self.assertIn('recording.post_process.pre_output_timeout_seconds',
                      groups['conversion-resilience'])
        self.assertNotIn('recording.post_process.pre_output_timeout_seconds',
                         groups['conversion'])


class ConversionRestartDescriptionTests(unittest.TestCase):
    """Guards BUGS.md 2026-09-17 @ 06:26:58 AM "Settings said a stopped conversion restarts from scratch".

    An mp4 re-encode resumes from its last finished part (dev/changelog/955), so the
    auto-restart row must not promise a start-over for every conversion.
    """

    def test_auto_restart_row_describes_the_resume(self):
        t = make_test_app()
        try:
            html = t.app.test_client().get('/settings').get_data(as_text=True)
        finally:
            t.cleanup()
        row = re.search(r'data-path="recording\.post_process\.auto_restart"(.*?)<div class="fl-key">',
                        html, re.S)
        self.assertIsNotNone(row)
        self.assertNotIn('from scratch', row.group(1))
        self.assertIn('picks up from its last finished part', row.group(1))


class TierTests(unittest.TestCase):
    """DESIGN.md 15.9: the Basic/Advanced view (dev/changelog/1005)."""

    BASIC = {
        'display.timezone', 'display.time_format', 'display.guide_collapse_gaps',
        'recording.dvr_output_dir', 'recording.images_dir', 'recording.filename_template', 'recording.retention_days',
        'recording.retention_delete_file', 'recording.post_process.enabled',
        'recording.post_process.format', 'recording.post_process.reencode_mode',
        'recording.move_on_complete.enabled', 'recording.move_on_complete.destination',
        'recording.live_thumbnail.enabled', 'recording.logo_cache.enabled', 'ffmpeg.path',
        'sync.sync_interval_hours', 'sync.epg_days_ahead', 'channel_testing.screenshots_enabled',
        'channel_testing.window.start', 'channel_testing.window.end', 'auth.enabled',
        'integrations.home_assistant.enabled',
    }

    @classmethod
    def setUpClass(cls):
        from app.routes.settings import SETTINGS_VIEW_PREF
        cls.t = make_test_app()
        cls.t.app.config['WTF_CSRF_ENABLED'] = False
        client = cls.t.app.test_client()
        cls.basic_html = client.get('/settings').get_data(as_text=True)
        cls.notif_html = client.get('/settings/notifications').get_data(as_text=True)
        client.post(f'/api/user-prefs/{SETTINGS_VIEW_PREF}', json={'value': True})
        cls.advanced_html = client.get('/settings').get_data(as_text=True)
        client.post(f'/api/user-prefs/{SETTINGS_VIEW_PREF}', json={'value': False})
        cls.back_html = client.get('/settings').get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    @staticmethod
    def _tiers(html):
        return dict(re.findall(r'data-path="([^"]+)" data-tier="([^"]*)"', html))

    def test_the_basic_set_is_exactly_the_approved_one(self):
        tiers = self._tiers(self.basic_html)
        self.assertEqual(len(tiers), 115)
        self.assertEqual({p for p, t in tiers.items() if t == 'basic'}, self.BASIC)
        self.assertEqual({t for t in tiers.values()}, {'basic', 'advanced'})

    def test_every_advanced_row_carries_its_badge_and_no_basic_row_does(self):
        rows = re.findall(r'data-path="([^"]+)" data-tier="[^"]*".*?<div class="fl-label">(.*?)</div>',
                          self.basic_html, re.S)
        self.assertEqual(len(rows), 115)
        for path, badge in rows:
            with self.subTest(path=path):
                self.assertEqual('tier-badge' in badge, path not in self.BASIC)

    def test_the_forms_filter_as_basic_units(self):
        self.assertEqual(self.basic_html.count('<div data-tier="basic">'), 2)

    def test_the_first_paint_follows_the_saved_view(self):
        self.assertIn('class="set-page" data-view="basic"', self.basic_html)
        self.assertIn('class="set-page" data-view="advanced"', self.advanced_html)
        self.assertIn('class="set-page" data-view="basic"', self.back_html)
        self.assertRegex(self.advanced_html,
                         r'class="tierseg-opt on" data-view-set="advanced" role="radio" aria-checked="true"')

    def test_advanced_rows_are_hidden_before_the_script_runs(self):
        self.assertIn('.set-page[data-view="basic"]:not(.ready) .frow[data-tier="advanced"] { display: none; }',
                      self.basic_html)

    def test_notifications_rows_declare_a_tier_and_the_page_has_no_switch(self):
        self.assertEqual(self._tiers(self.notif_html),
                         {'notifications.push_rate_limit_seconds': 'advanced',
                          'notifications.base_url': 'basic'})
        self.assertNotIn('data-view-set', self.notif_html)


class ChangedFromDefaultTests(unittest.TestCase):
    """DESIGN.md 15.9: the changed-from-default mark and chip (dev/changelog/1006).

    make_test_app() sandboxes config.yaml to a file holding nothing, so every row starts at
    its default and each case writes the change it is about."""

    def setUp(self):
        from app import config as cfgmod
        self.cfgmod = cfgmod
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def _write(self, data):
        from tests.support.app import write_sandbox_config
        write_sandbox_config(self.cfgmod._CONFIG_PATH,
                             {'config_version': self.cfgmod.CURRENT_CONFIG_VERSION, **data})
        self.cfgmod._yaml_cache = None

    def _page(self):
        return self.client.get('/settings').get_data(as_text=True)

    @staticmethod
    def _css():
        import os
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'static', 'css', 'style.css'), encoding='utf-8') as f:
            return f.read()

    @staticmethod
    def _row(html, path):
        m = re.search(r'<div class="frow[^"]*" data-path="%s"[^>]*>.*?<div class="fr-changed">' % re.escape(path),
                      html, re.S)
        return m.group(0) if m else None

    def test_nothing_is_marked_on_a_config_that_sets_nothing(self):
        html = self._page()
        self.assertNotIn('data-changed', html)
        self.assertIn('id="schanged"', html)
        self.assertIn('Changed from default <span class="chip-count" id="schanged-n"></span>', html)

    def test_a_changed_row_is_marked_and_its_default_line_is_untouched(self):
        self._write({'recording': {'retention_days': 30}})
        row = self._row(self._page(), 'recording.retention_days')
        self.assertIn('data-changed="true"', row)
        self.assertIn('<div class="fl-default">Default: <code>0</code></div>', row)
        self.assertIn('<div class="fr-side"><div class="fr-ctl">', row)

    def test_every_setting_row_carries_the_mark_and_the_attribute_turns_it_on(self):
        html = self._page()
        self.assertEqual(html.count('<div class="fr-changed">Changed from default</div>'), 115)
        self.assertIn('.frow[data-changed] .fr-changed { display: inline-flex; }', self._css())

    def test_a_value_saved_equal_to_its_default_is_not_a_change(self):
        self._write({'recording': {'retention_days': 0.0}, 'display': {'guide_collapse_gaps': True}})
        html = self._page()
        self.assertNotIn('data-changed', html)

    def test_the_helper_compares_the_merged_config_and_returns_paths_only(self):
        cfg = self.cfgmod.load_config()
        cfg['recording']['retention_days'] = 7
        cfg['flask']['secret_key'] = 'a-real-secret-value'
        cfg['not_a_setting'] = {'x': 1}
        out = self.cfgmod.changed_from_default(cfg)
        self.assertEqual(sorted(out), ['flask.secret_key', 'recording.retention_days'])

    def test_a_save_reports_whether_the_field_still_differs(self):
        r = self.client.post('/api/settings/field', json={'path': 'recording.retention_days', 'value': 14})
        self.assertIs(r.get_json()['changed_from_default'], True)
        self.assertIn('data-changed="true"', self._row(self._page(), 'recording.retention_days'))
        r = self.client.post('/api/settings/field', json={'path': 'recording.retention_days', 'value': 0})
        self.assertIs(r.get_json()['changed_from_default'], False)
        self.assertNotIn('data-changed', self._row(self._page(), 'recording.retention_days'))

    def test_the_filename_designer_save_reports_it_too(self):
        r = self.client.post('/api/filename-template', json={'template': '{title}'})
        self.assertIs(r.get_json()['changed_from_default'], True)
        default = self.cfgmod.config_default('recording.filename_template')
        r = self.client.post('/api/filename-template', json={'template': default})
        self.assertIs(r.get_json()['changed_from_default'], False)

    def test_a_changed_read_only_row_is_marked(self):
        self._write({'flask': {'port': 5050}})
        html = self._page()
        self.assertIn('data-changed="true"', self._row(html, 'flask.port'))
        self.assertNotIn('data-changed', self._row(html, 'flask.host'))

    def test_a_changed_secret_is_marked_and_its_value_never_rendered(self):
        self._write({'flask': {'secret_key': 'sekrit-value-that-must-not-render'}})
        html = self._page()
        self.assertIn('data-changed="true"', self._row(html, 'flask.secret_key'))
        self.assertNotIn('sekrit-value-that-must-not-render', html)

    def test_notifications_rows_carry_no_mark(self):
        self._write({'notifications': {'push_rate_limit_seconds': 5}})
        html = self.client.get('/settings/notifications').get_data(as_text=True)
        self.assertNotIn('data-changed', html)
        self.assertNotIn('id="schanged"', html)


PP = 'recording.post_process'


class GatingAndOverrideTests(unittest.TestCase):
    """DESIGN.md 15.9: gated rows and override badges (dev/changelog/1007).

    Each gate is a claim about what app/ reads, so the exact table is the ruling: a row
    missing from it never dims, and a row added to it dims a setting something still uses."""

    SAFE_TO_DIM = {
        f'{PP}.format': [f'{PP}.enabled'],
        f'{PP}.delete_source': [f'{PP}.enabled'],
        f'{PP}.pre_output_timeout_seconds': [f'{PP}.enabled'],
        f'{PP}.auto_restart': [f'{PP}.enabled'],
        f'{PP}.stall_seconds': [f'{PP}.enabled'],
        f'{PP}.progress_interval_seconds': [f'{PP}.enabled'],
        f'{PP}.collision_policy': [f'{PP}.enabled'],
        # An MKV conversion is a plain stream copy: no re-encode decision, no CRF, no AAC.
        f'{PP}.reencode_mode': [f'{PP}.enabled', f'{PP}.format!=mkv'],
        f'{PP}.audio_bitrate_kbps': [f'{PP}.enabled', f'{PP}.format!=mkv'],
        f'{PP}.video_crf': [f'{PP}.enabled', f'{PP}.format!=mkv', f'{PP}.reencode_mode!=never'],
        f'{PP}.max_restart_attempts': [f'{PP}.enabled', f'{PP}.auto_restart'],
        f'{PP}.collision_lookahead_multiplier': [f'{PP}.enabled', f'{PP}.collision_policy!=off'],
        'recording.move_on_complete.destination': ['recording.move_on_complete.enabled'],
        'recording.post_script.path': ['recording.post_script.enabled'],
        'recording.live_thumbnail.auto_refresh_seconds': ['recording.live_thumbnail.enabled'],
        'auth.session_timeout_minutes': ['auth.enabled'],
        'config_backup.backup_hour_et': ['config_backup.enabled'],
    }

    # Each is read with its apparent gate off, so dimming it would be a false claim.
    LOOKS_GATED_IS_NOT = (
        'recording.retention_delete_file', 'watchdog.stall_move_window_minutes',
        'channel_testing.screenshots_keep_count',
        'channel_testing.pre_check.lead_minutes', 'channel_testing.pre_check.retry_minutes',
        'channel_testing.pre_check.min_margin_seconds', 'auth.cookie_secure',
        'config_backup.backup_retention_days', 'config_backup.backup_dir',
        'sync.skip_sync_if_recording_within_minutes',
        'channel_testing.skip_if_recording_within_minutes',
        # An account's own debug switch uses the dump tools with the global one off.
        'debug.xtream_dump_dir',
    )

    OVERRIDES = {
        'recording_profile': ('recording.filename_template', 'recording.retention_days',
                              'watchdog.stall_timeout_seconds', 'watchdog.restart_delay_seconds',
                              'watchdog.max_consecutive_failures', 'watchdog.stall_move_count',
                              'watchdog.stall_move_window_minutes',
                              'channel_testing.pre_check.enabled'),
        'health_check_profile': ('channel_testing.test_duration_seconds',
                                 'channel_testing.wait_between_channels_seconds',
                                 'channel_testing.screenshots_enabled',
                                 'channel_testing.connect_retries',
                                 'channel_testing.connect_timeout_seconds',
                                 'channel_testing.connect_retry_delay_seconds'),
        'channel': ('ffmpeg.pace_realtime',),
        'account': ('accounts.default_max_connections', 'sync.sync_interval_hours',
                    'sync.url_normalization', 'debug.xtream_debug_mode'),
    }

    @classmethod
    def setUpClass(cls):
        t = make_test_app()
        try:
            cls.html = t.app.test_client().get('/settings').get_data(as_text=True)
        finally:
            t.cleanup()
        cls.rows = {}
        for m in re.finditer(r'<div class="frow[^"]*" data-path="([^"]+)"[^>]*>(.*?)<div class="fl-desc">',
                             cls.html, re.S):
            cls.rows[m.group(1)] = m.group(0)

    def test_exactly_the_safe_to_dim_rows_declare_their_gates(self):
        declared = {}
        for path, head in self.rows.items():
            m = re.search(r'data-gated-by="([^"]*)"', head)
            if m:
                declared[path] = m.group(1).split(' ')
        self.assertEqual(declared, self.SAFE_TO_DIM)

    def test_rows_that_only_look_gated_do_not_dim(self):
        for path in self.LOOKS_GATED_IS_NOT:
            with self.subTest(path=path):
                self.assertIn(path, self.rows)
                self.assertNotIn('data-gated-by', self.rows[path])

    def test_every_gated_row_carries_a_hidden_empty_gate_badge_in_its_label_line(self):
        for path in self.SAFE_TO_DIM:
            with self.subTest(path=path):
                self.assertIn(' <span class="gate-badge" hidden></span>', self.rows[path])
        self.assertEqual(self.html.count('class="gate-badge"'), len(self.SAFE_TO_DIM))

    def test_the_server_renders_no_row_dimmed(self):
        # The first paint is the script's; a server-rendered dim would need JS to undo it.
        self.assertNotIn('gated"', self.html.split('<div class="set-body"', 1)[1].split('<script', 1)[0])

    def test_exactly_the_overridable_rows_carry_a_badge_linking_to_where_the_override_lives(self):
        badge = {'recording_profile': ('/profiles', 'Profile can override'),
                 'health_check_profile': ('/health-check-profiles', 'Profile can override'),
                 'channel': ('/channels', 'Channel can override'),
                 'account': ('/accounts', 'Account can override')}
        found = {}
        for path, head in self.rows.items():
            m = re.search(r'<a class="override-badge" href="([^"]*)">([^<]*)</a>', head)
            if m:
                found[path] = (m.group(1), m.group(2))
        expected = {p: badge[k] for k, paths in self.OVERRIDES.items() for p in paths}
        self.assertEqual(found, expected)

    def test_descriptions_no_longer_restate_the_badge(self):
        for phrase in ('Overridable per Recording Profile', 'for accounts with no per-account override',
                       'Per-account setting overrides this', 'A recording profile can override this',
                       'Each channel can override this'):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, self.html)

    def test_the_dim_keeps_the_input_editable(self):
        # Dim, never disable: setting a destination before flipping its switch is the natural
        # order, and a disabled input would force switch-first.
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'templates', 'settings.html'), encoding='utf-8') as f:
            rules = re.findall(r'\.frow\.gated[^{]*\{([^}]*)\}', f.read())
        self.assertEqual(len(rules), 1)
        for body in rules:
            self.assertNotIn('pointer-events', body)
            self.assertNotIn('display: none', body)

if __name__ == '__main__':
    unittest.main()
