"""The app shell's nav: collapsed rail, folding sections, chip group, the retired Setup flyout.

Guards the production port of mockups 09 (desktop, round 7) and 10 (mobile, round 4)
- see dev/changelog/383. Each assertion is a thing that broke, or would silently
break, if the port is edited carelessly:

  * `<body class="nav-min">` is server-rendered from the `nav_collapsed` pref. That
    is the whole reason persistence is server-side rather than localStorage: the
    class has to be in the first paint or every navigation flashes the expanded
    sidebar.
    Absent when the pref is unset - server-rendered initial state equals the
    "nothing active" state (CLAUDE.md, Frontend rendering).
  * No `setup-flyout` / `setup-trigger` / `setup_flyout` token survives anywhere.
    Teardown releases everything the create path acquired: markup, CSS and script.
  * Both shells render the same icon set from one definition, and the sidebar
    renders exactly the production destinations in order: the everyday pages, then
    the three sections of DESIGN.md §2 (amended 2026-10-31): Channels & guide plain,
    Setup and System folding, each with a hint line held to its links and folded on every
    page load except the one holding the page being shown (dev/changelog/1372) - no
    Health Checks row, which lives on the Channel Groups tab (DESIGN.md §14.1) even
    though both mockups drew it.
  * The sys-stats block is in the sidebar and NOT in the mobile drawer (it is the
    top bar's stats chip there, DESIGN.md 9.7 amended 2026-07-29).
  * The desktop `.topbar` renders inside `.colmain` with the `<aside>` outside it -
    the containment that makes the sidebar start at the top of the page - and the
    bar declares no position (deliberately not sticky).
  * Three chips in the order stats, recording, background, in both bars, carrying
    no ids: the group renders twice, and duplicate ids would leave getElementById
    updating only the first copy.
  * No `activity-dot` / `rec-dot` / `bg-dot` token survives - the chips are those
    indicators now.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_nav_shell
"""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import make_test_app  # noqa: E402
from app import db  # noqa: E402
from app.database import UserPref  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The destinations the production sidebar renders, in order (DESIGN.md §2, amended
# 2026-10-31, mockup 48, dev/changelog/1363): the everyday pages with no heading above them,
# then three folding sections. Health Checks is deliberately absent (DESIGN.md §14.1); EPG
# Browser was retired (dev/changelog/631). Maintenance sits second to last, between Logs and
# Settings (DESIGN.md §16.1, dev/changelog/444); Readiness sits beside Alerts, the other link
# carrying red and amber counts (dev/changelog/1366). Auto-record is set up once and left to run,
# so it is in Setup, and the Recordings header links to it.
NAV_LABELS = [
    # Always in the markup, hidden once setup is done (dev/changelog/1280).
    'Get started',
    'Dashboard', 'TV Guide', 'Search', 'Recordings',
    'Channel Groups', 'Hide Channels', 'Tags', 'EPG Manager',
    'Accounts', 'Auto-record', 'Recording Profiles', 'Health Check Profiles',
    'Alerts', 'Readiness', 'Jobs', 'Logs', 'Maintenance', 'Settings',
]

# The folding sections, in order: (key, heading, the labels of the links inside).
NAV_SECTIONS = [
    ('setup', 'Setup', ['Accounts', 'Auto-record', 'Recording Profiles', 'Health Check Profiles']),
    ('system', 'System', ['Alerts', 'Readiness', 'Jobs', 'Logs', 'Maintenance', 'Settings']),
]
# The one section that does not fold (dev/changelog/1372).
PLAIN_SECTION = ('Channels &amp; guide', ['Channel Groups', 'Hide Channels', 'Tags', 'EPG Manager'])

_SEC_RE = re.compile(
    r'<div class="nav-sec( is-folded)?" data-fold="(\w+)">\s*'
    r'<button type="button" class="nav-section nav-fold" aria-expanded="(\w+)">'
    r'<span class="nav-fh"><span class="nav-t">([^<]+)</span>.*?<span class="nav-hint">(.*?)</span></button>'
    r'\s*<div class="nav-sec-body">(.*?)</div>\s*</div>', re.S)

_PLAIN_RE = re.compile(
    r'<div class="nav-sec">\s*<div class="nav-section"><span class="nav-t">([^<]+)</span></div>'
    r'(.*?)</div>\s*<div class="nav-sec', re.S)


def _sections(shell):
    """[{key, head, folded, expanded, hint, links}] for one shell's folding sections."""
    return [{'key': m.group(2), 'head': m.group(4), 'folded': bool(m.group(1)),
             'expanded': m.group(3),
             'hint': re.findall(r'<span class="nav-hn">([^<]+)</span>', m.group(5)),
             'links': re.findall(r'<span class="nav-t">([^<]+)</span>', m.group(6))}
            for m in _SEC_RE.finditer(shell)]


def _plain_sections(shell):
    """[(heading, links)] for one shell's sections that do not fold."""
    return [(m.group(1), re.findall(r'<span class="nav-t">([^<]+)</span>', m.group(2)))
            for m in _PLAIN_RE.finditer(shell)]


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


def _slice(html, start_marker, end_marker):
    """The substring between two markers, so a check can be scoped to one shell."""
    i = html.index(start_marker)
    j = html.index(end_marker, i)
    return html[i:j]


class NavCollapsedRendersServerSideTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _body_tag(self):
        html = self.t.client.get('/').get_data(as_text=True)
        return re.search(r'<body[^>]*>', html).group(0)

    def test_no_pref_renders_no_nav_min_class(self):
        self.assertNotIn('nav-min', self._body_tag())

    def test_pref_true_renders_nav_min_class(self):
        db.session.add(UserPref(key='nav_collapsed', value=json.dumps(True)))
        db.session.commit()
        self.assertIn('class="nav-min"', self._body_tag())

    def test_pref_false_renders_no_nav_min_class(self):
        db.session.add(UserPref(key='nav_collapsed', value=json.dumps(False)))
        db.session.commit()
        self.assertNotIn('nav-min', self._body_tag())

    def test_corrupt_pref_falls_back_to_expanded(self):
        """A bad value must not 500 the whole app - every page renders this."""
        db.session.add(UserPref(key='nav_collapsed', value='{not json'))
        db.session.commit()
        resp = self.t.client.get('/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('nav-min', self._body_tag())

    def test_collapse_state_round_trips_through_the_prefs_route(self):
        """The button persists through /api/user-prefs, so the next page load
        renders collapsed. The underscore in the key has to survive that route's
        own key validation, which is why the round trip is asserted and not just
        the read."""
        self.t.app.config['WTF_CSRF_ENABLED'] = False  # jsonFetch supplies the token in the app
        resp = self.t.client.post('/api/user-prefs/nav_collapsed',
                                  json={'value': True})
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        self.assertIn('class="nav-min"', self._body_tag())


class NavFoldsRenderServerSideTests(unittest.TestCase):
    """Setup and System are folded on every page load except the one holding the page being
    shown, and nothing remembers a fold or an open (DESIGN.md §2, amended 2026-10-31,
    dev/changelog/1372): a new user who walked through setup must not be left with both
    sections open for good."""

    def setUp(self):
        self.t = make_test_app(csrf=False)

    def tearDown(self):
        self.t.cleanup()

    def _state(self, path='/'):
        html = self.t.client.get(path).get_data(as_text=True)
        sidebar = _slice(html, '<aside class="sidebar">', '</aside>')
        return {s['key']: s['folded'] for s in _sections(sidebar)}

    def test_every_page_load_starts_folded(self):
        self.assertEqual(self._state(), {'setup': True, 'system': True})

    def test_the_page_being_shown_opens_its_section_on_that_page_only(self):
        self.assertEqual(self._state('/settings'), {'setup': True, 'system': False})
        self.assertEqual(self._state('/rules'), {'setup': False, 'system': True})
        self.assertEqual(self._state('/'), {'setup': True, 'system': True})

    def test_a_stored_fold_pref_changes_nothing(self):
        """An install that ran the remembered folds has a `nav_folds` row, possibly saved
        open by the walkthrough and carrying the retired `chan` key. It is ignored."""
        db.session.add(UserPref(key='nav_folds', value=json.dumps(
            {'chan': True, 'setup': False, 'system': False})))
        db.session.commit()
        html = self.t.client.get('/').get_data(as_text=True)
        sidebar = _slice(html, '<aside class="sidebar">', '</aside>')
        self.assertEqual({s['key']: s['folded'] for s in _sections(sidebar)},
                         {'setup': True, 'system': True})
        self.assertEqual(_plain_sections(sidebar), [PLAIN_SECTION])

    def test_a_shortcut_is_lit_on_its_own_page(self):
        for path, lit in (('/guide', '/guide'), ('/channels', '/channels'), ('/tags', None)):
            bar = _slice(self.t.client.get(path).get_data(as_text=True), '<nav class="topnav">', '</nav>')
            active = re.findall(r'<a class="topnav-sc active" href="([^"]+)"', bar)
            self.assertEqual(active, [lit] if lit else [], path)


class SearchPageNameTests(unittest.TestCase):
    """Channel Search is Search, in the menu and on the page (dev/changelog/1363)."""

    def test_the_page_is_called_search(self):
        t = make_test_app()
        try:
            html = t.client.get('/channels').get_data(as_text=True)
        finally:
            t.cleanup()
        self.assertIn('<title>Search - ChannelBin</title>', html)
        self.assertEqual(re.findall(r'<h1>([^<]*)</h1>', html), ['Search'])
        self.assertNotIn('Channel Search<', html)


class RecordingsHeaderTests(unittest.TestCase):
    """The Recordings header, the same at every width (DESIGN.md §9.3, amended 2026-10-31):
    "Auto-record ->" and "Schedule recording" with a caret, both primary. The phone's
    "TV Guide ->" stand-in is gone; the top bar's TV Guide shortcut covers it."""

    @classmethod
    def setUpClass(cls):
        cls.t = make_test_app()
        cls.html = cls.t.client.get('/recordings').get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def _head(self):
        return _slice(self.html, '<div class="page-head">', '<div class="empty-state">')

    def test_auto_record_is_a_primary_link_to_the_rules(self):
        self.assertRegex(self._head(), r'<a href="/rules" class="btn btn-primary">Auto-record '
                                       r'<span class="arw"><svg class="icon" data-icon="forward"')

    def test_schedule_recording_is_a_primary_menu_with_a_caret_and_no_plus(self):
        head = self._head()
        self.assertRegex(head, r'<button type="button" class="btn btn-primary" data-menu data-sched-menu'
                               r'[^>]*>Schedule recording <svg class="icon btn-caret" data-icon="dropdown"')
        menu = _slice(head, '<div class="menu">', '</div>')
        self.assertEqual(re.findall(r'<a href="([^"]+)">.*?</svg> ([^<]+)</a>', menu),
                         [('/guide', 'From the TV Guide'),
                          ('/channels?grain=airings', 'From a search'),
                          ('/recordings/new', 'Manual recording')])
        self.assertNotIn('+ Schedule', self.html)

    def test_the_phone_swap_is_gone(self):
        css = _read('static/css/style.css')
        for token in ('head-action-guide', 'head-action-schedule'):
            self.assertNotIn(token, self.html)
            self.assertNotIn(token, css)


class RenderedShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One app and one render for the whole class: every case reads the markup and
        # none rewrites the state it was rendered from (dev/changelog/979).
        cls.t = make_test_app()
        cls.html = cls.t.client.get('/').get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def test_no_setup_flyout_token_survives(self):
        for token in ('setup-flyout', 'setup-trigger', 'setup-caret', 'setup_flyout'):
            self.assertNotIn(token, self.html, f'{token} still rendered')

    def test_no_activity_dot_token_survives(self):
        for token in ('activity-dot', 'rec-dot', 'bg-dot', 'dot-breathe'):
            self.assertNotIn(token, self.html, f'{token} still rendered')

    def test_every_section_renders_in_both_shells_with_its_links(self):
        for shell in (self._sidebar(), self._drawer()):
            got = [(sec['key'], sec['head'], sec['links']) for sec in _sections(shell)]
            self.assertEqual(got, NAV_SECTIONS)

    def test_channels_and_guide_is_a_plain_section_that_does_not_fold(self):
        """dev/changelog/1372: a heading over its four pages, with no fold button, chevron
        or hint line, in both shells, and ahead of the two that fold."""
        for shell in (self._sidebar(), self._drawer()):
            self.assertEqual(_plain_sections(shell), [PLAIN_SECTION])
            self.assertEqual(shell.count('class="nav-section nav-fold"'), len(NAV_SECTIONS))
            self.assertLess(shell.index('Channels &amp; guide'), shell.index('data-fold="setup"'))
        self.assertNotIn('data-fold="chan"', self.html)

    def test_each_folded_hint_names_its_sections_links_in_order(self):
        """The hint is a second list of the same names, so it is held to the links."""
        for shell in (self._sidebar(), self._drawer()):
            for sec in _sections(shell):
                self.assertEqual(sec['hint'], sec['links'], sec['key'])

    def _sidebar(self):
        return _slice(self.html, '<aside class="sidebar">', '</aside>')

    def _drawer(self):
        return _slice(self.html, 'id="topnav-menu"', '</div>\n\n  <div class="shell">')

    def test_sidebar_renders_exactly_the_production_destinations(self):
        labels = re.findall(r'<span class="nav-t">([^<]+)</span>', self._sidebar())
        # brand + 3 section headings + the destinations
        self.assertEqual([l for l in labels if l in NAV_LABELS], NAV_LABELS)
        self.assertNotIn('Health Checks<', self._sidebar())
        for retired in ('Channel Search', 'Hide Rules', 'Library'):
            self.assertNotIn(f'>{retired}<', self.html)

    def test_the_everyday_pages_lead_above_every_section(self):
        """DESIGN.md §2, amended 2026-10-31: Get started, Dashboard, TV Guide, Search and
        Recordings sit above the first heading. Both shells, since nav_sections() renders
        both (§9.7)."""
        for shell in (self._sidebar(), self._drawer()):
            first = shell.index('class="nav-sec')
            for label in NAV_LABELS[:5]:
                self.assertLess(shell.index(f'>{label}</span>'), first, label)

    def test_sections_start_folded_where_the_design_put_them(self):
        """Setup and System folded in both shells. The Dashboard is in no section, so none
        is opened for it."""
        for shell in (self._sidebar(), self._drawer()):
            got = {s['key']: (s['folded'], s['expanded']) for s in _sections(shell)}
            self.assertEqual(got, {'setup': (True, 'false'), 'system': (True, 'false')})

    def test_the_phone_bar_has_the_guide_and_search_shortcuts(self):
        bar = _slice(self.html, '<nav class="topnav">', '</nav>')
        self.assertEqual(re.findall(r'<a class="topnav-sc" href="([^"]+)" aria-label="([^"]+)">', bar),
                         [('/guide', 'TV Guide'), ('/channels', 'Search')])
        self.assertLess(bar.index('class="topnav-sc"'), bar.index('class="nav-indicators"'))

    def test_both_shells_render_the_same_icon_set(self):
        # Every destination draws its registry icon with icon(name, 'nav-i') (app/icons.py).
        icons_of = lambda s: re.findall(r'<svg class="(?:icon )?nav-i[^"]*"[^>]*>(.*?)</svg>', s, re.S)
        sidebar_icons = icons_of(self._sidebar())
        drawer_icons = icons_of(self._drawer())
        self.assertEqual(len(sidebar_icons), len(NAV_LABELS))
        self.assertEqual(sidebar_icons, drawer_icons)

    def test_sys_mini_is_in_the_sidebar_and_not_in_the_drawer(self):
        self.assertIn('class="sys-mini"', self._sidebar())
        self.assertNotIn('sys-mini', self._drawer())

    def test_topbar_is_inside_colmain_and_the_sidebar_is_not(self):
        shell = _slice(self.html, '<div class="shell">', '<footer')
        colmain = shell.index('<div class="colmain">')
        self.assertLess(shell.index('<aside class="sidebar">'), colmain)
        self.assertLess(colmain, shell.index('<div class="topbar">'))
        self.assertLess(shell.index('<div class="topbar">'), shell.index('<main class="main'))

    def test_both_bars_render_three_chips_in_order_and_without_ids(self):
        for bar in (_slice(self.html, '<nav class="topnav">', '</nav>'),
                    _slice(self.html, '<div class="topbar">', '<main class="main')):
            chips = re.findall(r'class="actchip (chip-\w+)"', bar)
            self.assertEqual(chips, ['chip-stats', 'chip-rec', 'chip-job'])
            self.assertNotIn('id="chip', bar)

    def test_hamburger_precedes_the_brand_in_the_mobile_bar(self):
        bar = _slice(self.html, '<nav class="topnav">', '</nav>')
        self.assertLess(bar.index('id="topnav-toggle"'), bar.index('class="brand"'))

    def test_collapse_button_renders_once_in_the_sidebar_brand_row(self):
        row = _slice(self._sidebar(), '<div class="brand-row">', '</div>')
        self.assertIn('class="brand"', row)
        self.assertIn('<hr class="brand-rule">', row)
        self.assertIn('class="navcollapse"', row)
        self.assertEqual(self.html.count('class="navcollapse"'), 1)

    def test_count_badges_carry_a_rail_pip(self):
        """The rail hides the badge, so the pip is the only mark left; a link with
        a count must have one, and the dynamic ones start hidden with it.

        Four: Recordings, Dashboard, Alerts, and Maintenance - the last one added with the
        Readiness check's blocked/degraded counts (dev/changelog/950).
        """
        sidebar = self._sidebar()
        self.assertEqual(sidebar.count('class="nav-pip'), 4)
        self.assertIn('<span class="nav-pip pip-alert" style="display:none"></span>', sidebar)
        self.assertIn('<span class="nav-pip pip-ready" style="display:none"></span>', sidebar)

    def test_alerts_link_carries_a_red_and_a_yellow_count_in_both_shells(self):
        """dev/changelog/924: one grey count of every unread alert became a red count
        (ERROR + CRIT) and a yellow one (WARN). Both start hidden - the "nothing active"
        state - and each names its unit so the rail tip can say which number is which."""
        pair = ('<span class="nav-count nav-count-bad" data-unit="error" style="display:none"></span>'
                '<span class="nav-count nav-count-warn" data-unit="warning" style="display:none"></span>')
        for shell in (self._sidebar(), self._drawer()):
            self.assertIn(pair, shell)
        self.assertNotIn('nav-count-alerts', self.html)


class ShellSourceTests(unittest.TestCase):
    """Checks that belong to the source rather than to one rendered page."""

    def test_stylesheet_drops_every_retired_rule(self):
        css = _read('static/css/style.css')
        for token in ('.setup-trigger', '.setup-flyout', '.setup-caret',
                      '.activity-dot', '.rec-dot', '.bg-dot', 'dot-breathe',
                      '.topnav-menu .sys-mini'):
            self.assertNotIn(token, css, f'{token} rule still present')

    def test_topbar_declares_no_position_or_z_index(self):
        """The desktop bar is not sticky. The sticky sidebar beside it is
        exactly why someone would add one back."""
        css = _read('static/css/style.css')
        rule = _slice(css, '.topbar {', '}')
        self.assertNotIn('position', rule)
        self.assertNotIn('z-index', rule)

    def test_rail_rules_are_gated_to_the_desktop_breakpoint(self):
        """body.nav-min hides .nav-t, which the mobile drawer also uses. Ungated,
        a persisted collapse would blank every label in the drawer."""
        css = _read('static/css/style.css')
        block = _slice(css, '@media (min-width: 961px) {', '\n}')
        for sel in ('body.nav-min .nav-t', 'body.nav-min .sidebar',
                    'body.nav-min .sys-mini'):
            self.assertIn(sel, block)
        outside = css.replace(block, '')
        self.assertNotIn('body.nav-min', outside)

    def test_chip_click_is_keyed_on_pinned_not_on_open(self):
        """dev/docs/BUGS.md 2026-07-29 09:41 AM. mouseenter fires before click on a
        real pointer, and touch synthesizes hover before the tap, so a click handler
        that closes when the tip is *already open* closes the one hover just opened -
        the tip opens and closes inside a single tick and a phone can never reach the
        numbers. The close condition has to be an explicit pin flag.

        Source-shape guard, not a behavioral one: this is inline DOM code in
        base.html and the repo has no jsdom to drive it (the mockup harnesses ask
        you to install it out-of-tree). It still bites - the defective version
        spelled the condition `activeChip === el`.

        All three chips take this path since dev/changelog/949; which chips pin is
        tests/test_activity_chip_links.py's question, this one is about how."""
        base = _read('templates/base.html')
        click_handler = _slice(base, "el.addEventListener('click'", "el.addEventListener('mouseenter'")
        self.assertIn('dataset.pinned', click_handler)
        self.assertNotIn('activeChip === el', click_handler)
        # hover-out must not close a pinned tip either, or the pin lasts until the
        # pointer moves one pixel off the chip
        leave = _slice(base, "el.addEventListener('mouseleave'", '}\n\n    chip')
        self.assertIn('!el.dataset.pinned', leave)

    def test_activity_chip_height_is_fixed_not_auto(self):
        """dev/docs/BUGS.md 2026-08-05 @ 05:55:21 AM ET: the three chip states (an icon, a
        count number, or the dim state's plain dot) have different intrinsic content
        heights, so an auto-sized pill visibly shrank whenever the dim dot was the only
        content - a coupled-value CSS regression (CLAUDE.md CSS rule). The fix is a fixed
        height shared by every state, not per-state padding/line-height tuning that could
        drift out of sync again."""
        css = _read('static/css/style.css')
        rule = _slice(css, '\n.actchip {', '}')
        self.assertIn('height:', rule)

    def test_activity_chip_dim_state_is_reachable(self):
        """dev/docs/BUGS.md 2026-08-05 (the dim revival): applyChip must branch on the
        server's active/dim/hidden state explicitly, not derive visibility from a bare
        count - a dim chip carries count 0 by design (nothing active yet), so a
        count-based `display: none` at zero silently re-hides it forever, the exact
        defect that made the tooltip's dim branch unreachable in the first place."""
        base = _read('templates/base.html')
        chip_fn = _slice(base, 'function applyChip(els, mode, count) {', 'function applyIndicators(d) {')
        self.assertIn("mode === 'dim'", chip_fn)
        self.assertIn("mode === 'active'", chip_fn)
        self.assertIn("classList.add('dim')", chip_fn)
        indicators_fn = _slice(base, 'function applyIndicators(d) {', '\n      // Refresh')
        self.assertIn('d.recording.state, d.recording.active.length', indicators_fn)
        self.assertIn('d.background.state, d.background.tasks.length', indicators_fn)

    def test_no_flyout_script_survives_in_base_template(self):
        base = _read('templates/base.html')
        for token in ('setup_links', 'setup_active', 'setup_flyout', 'activity_dots'):
            self.assertNotIn(token, base, f'{token} still in base.html')

    def test_stats_tip_puts_each_reading_on_its_own_line(self):
        """dev/docs/BUGS.md 2026-10-23 @ 09:00:52 AM: showTooltip() opens the tip as
        display:block, which overrides .dot-tooltip's flex column, so the stats tip's
        inline <span> lines ran together ("RAM 32%Disk 7.8 GB freev0.29.0"). Every line
        the builder pushes must be a block element of its own."""
        base = _read('templates/base.html')
        self.assertIn("tooltip.style.display = 'block'", base,
                      'showTooltip no longer forces block; re-check what this test guards')
        body = _slice(base, 'function buildStatsTip(d) {', 'return lines.join')
        pushed = re.findall(r"lines\.push\('(<\w+)", body)
        self.assertEqual(len(pushed), 4, body)
        for tag in pushed:
            self.assertEqual(tag, '<div', f'a stats tip line opens with {tag}, which is inline')

    def test_mobile_drawer_opens_on_the_left(self):
        css = _read('static/css/style.css')
        block = _slice(css, '@media (min-width: 601px) and (max-width: 960px) {', '\n}')
        self.assertIn('left: 0', block)
        self.assertIn('right: auto', block)
        self.assertIn('border-right', block)
        self.assertNotIn('border-left', block)


class ContentWidthCapTests(unittest.TestCase):
    """The shell's content width - dev/changelog/733, DESIGN.md 2.

    The round-8 1600px cap (dev/changelog/384) is retired: every page now gets the
    width dashboard and guide always had, in both the expanded and collapsed sidebar
    states - the centered gutters it produced on ordinary list/table pages
    were the thing to remove, not a settled tradeoff. `.main-full` is gone with it,
    since there is nothing left for a page to opt out of.
    """

    def test_main_declares_no_content_cap(self):
        """Uncapped in both sidebar states - there is no longer a separate
        collapsed-state override because there is no cap to lift."""
        css = _read('static/css/style.css')
        # anchored at line start: `body.nav-min .main {` also contains `.main {`
        rule = _slice(css, '\n.main {', '}')
        self.assertNotIn('max-width', rule)

    def test_main_full_class_is_retired(self):
        """Superseded by uncapping `.main` itself - a page opting into full width
        via a second class is exactly the mechanism this change removed."""
        css = _read('static/css/style.css')
        self.assertIsNone(
            re.search(r'(?<![\w-])\.main-full(?![\w-])', css),
            '.main-full rule is back in style.css - .main has no cap to opt out of',
        )
        tpl_dir = os.path.join(_ROOT, 'templates')
        for dirpath, _dirnames, filenames in os.walk(tpl_dir):
            for name in filenames:
                if not name.endswith('.html'):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding='utf-8') as fh:
                    body = fh.read()
                self.assertNotIn(
                    'main-full', body,
                    f'{os.path.relpath(path, _ROOT)} still references the retired main-full class',
                )

    def test_shell_declares_no_second_content_cap(self):
        """`.container` was a second, wider (1800px) shell cap left over from the
        pre-sidebar top-navbar era, referenced by nothing. `.main` itself is
        uncapped now (dev/changelog/733) - nothing should reintroduce a shell-wide
        width limit under either name."""
        css = _read('static/css/style.css')
        self.assertIsNone(
            re.search(r'(?<![\w-])\.container(?![\w-])', css),
            '.container rule is back in style.css - the shell has no content cap',
        )
        # and nothing may start using it again by name
        tpl_dir = os.path.join(_ROOT, 'templates')
        for dirpath, _dirnames, filenames in os.walk(tpl_dir):
            for name in filenames:
                if not name.endswith('.html'):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding='utf-8') as fh:
                    body = fh.read()
                for attr in re.findall(r'class="([^"]*)"', body):
                    self.assertNotIn(
                        'container', attr.split(),
                        f'{os.path.relpath(path, _ROOT)} uses the deleted .container class',
                    )


if __name__ == '__main__':
    unittest.main()
