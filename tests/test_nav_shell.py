"""The app shell's nav: collapsed rail, chip group, and the retired Setup flyout.

Guards the production port of mockups 09 (desktop, round 7) and 10 (mobile, round 4)
- see dev/changelog/383. Each assertion is a thing that broke, or would silently
break, if the port is edited carelessly:

  * `<body class="nav-min">` is server-rendered from the `nav_collapsed` pref. That
    is the whole reason persistence is server-side rather than localStorage: the
    class has to be in the first paint or every navigation flashes a 212px sidebar.
    Absent when the pref is unset - server-rendered initial state equals the
    "nothing active" state (CLAUDE.md, Frontend rendering).
  * No `setup-flyout` / `setup-trigger` / `setup_flyout` token survives anywhere.
    Teardown releases everything the create path acquired: markup, CSS and script.
  * Both shells render the same icon set from one definition, and the sidebar
    renders exactly the production destinations in order, under the section
    headings of DESIGN.md §2 with Dashboard above all of them - no Health Checks
    row, which lives on the Channel Groups tab (DESIGN.md §14.1) even though both
    mockups drew it.
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

# The 15 destinations the production sidebar renders, in order. Health Checks is
# deliberately absent (DESIGN.md §14.1); EPG Browser was retired (dev/changelog/631).
# Maintenance sits second to last, between Logs and Settings (DESIGN.md §16.1,
# dev/changelog/444) - the slot is part of the approved design, so the order here is
# an assertion and not just a list. Hide Rules closes Setup rather than sitting on the
# Channels hub, whose tabs are on their way out (dev/changelog/782). Dashboard leads
# with no section heading above it and the former Channels section is gone, its two
# destinations folded into Library under their full names (DESIGN.md §2,
# dev/changelog/900).
NAV_LABELS = [
    'Dashboard',
    'Recordings', 'TV Guide', 'Channel Search', 'Channel Groups',
    'Accounts', 'Recording Profiles', 'Health Check Profiles', 'Tags', 'Hide Rules',
    'Jobs', 'Alerts', 'Logs', 'Maintenance', 'Settings',
]

# The section headings, in order. Dashboard sits above all three (DESIGN.md §2).
NAV_SECTIONS = ['Library', 'Setup', 'System']


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


class RenderedShellTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.html = self.t.client.get('/').get_data(as_text=True)

    def tearDown(self):
        self.t.cleanup()

    def test_no_setup_flyout_token_survives(self):
        for token in ('setup-flyout', 'setup-trigger', 'setup-caret', 'setup_flyout'):
            self.assertNotIn(token, self.html, f'{token} still rendered')

    def test_no_activity_dot_token_survives(self):
        for token in ('activity-dot', 'rec-dot', 'bg-dot', 'dot-breathe'):
            self.assertNotIn(token, self.html, f'{token} still rendered')

    def test_setup_links_render_inline_in_both_shells(self):
        for shell in (self._sidebar(), self._drawer()):
            for label in ('Accounts', 'Recording Profiles', 'Health Check Profiles', 'Tags',
                          'Hide Rules'):
                self.assertIn(f'>{label}</span>', shell)

    def _sidebar(self):
        return _slice(self.html, '<aside class="sidebar">', '</aside>')

    def _drawer(self):
        return _slice(self.html, 'id="topnav-menu"', '</div>\n\n  <div class="shell">')

    def test_sidebar_renders_exactly_the_15_production_destinations(self):
        labels = re.findall(r'<span class="nav-t">([^<]+)</span>', self._sidebar())
        # brand + 3 section headings + the destinations
        self.assertEqual([l for l in labels if l in NAV_LABELS], NAV_LABELS)
        self.assertNotIn('Health Checks<', self._sidebar())

    def test_dashboard_leads_the_nav_above_every_section_heading(self):
        """DESIGN.md §2: Dashboard is the landing page, so it is the one destination
        that is not a category of anything and sits above the first heading. Both
        shells, since nav_sections() renders both (§9.7)."""
        for shell in (self._sidebar(), self._drawer()):
            headings = re.findall(r'<div class="nav-section"><span class="nav-t">([^<]+)</span>',
                                  shell)
            self.assertEqual(headings, NAV_SECTIONS)
            self.assertLess(shell.index('>Dashboard</span>'), shell.index('class="nav-section"'))

    def test_both_shells_render_the_same_icon_set(self):
        icons_of = lambda s: re.findall(r'<svg class="nav-i"[^>]*>(.*?)</svg>', s, re.S)
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
        a count must have one, and the two dynamic ones start hidden with it."""
        sidebar = self._sidebar()
        self.assertEqual(sidebar.count('class="nav-pip'), 3)
        self.assertIn('<span class="nav-pip pip-alert" style="display:none"></span>', sidebar)

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
        block = _slice(css, '@media (min-width: 901px) {', '\n}')
        for sel in ('body.nav-min .nav-t', 'body.nav-min .sidebar',
                    'body.nav-min .sys-mini'):
            self.assertIn(sel, block)
        outside = css.replace(block, '')
        self.assertNotIn('body.nav-min', outside)

    def test_stats_chip_click_is_keyed_on_pinned_not_on_open(self):
        """dev/docs/BUGS.md 2026-07-29 09:41 AM. mouseenter fires before click on a
        real pointer, and touch synthesizes hover before the tap, so a click handler
        that closes when the tip is *already open* closes the one hover just opened -
        the tip opens and closes inside a single tick and a phone can never reach the
        numbers. The close condition has to be an explicit pin flag.

        Source-shape guard, not a behavioral one: this is inline DOM code in
        base.html and the repo has no jsdom to drive it (the mockup harnesses ask
        you to install it out-of-tree). It still bites - the defective version
        spelled the condition `activeChip === el`."""
        base = _read('templates/base.html')
        click_handler = _slice(base, "if (clickPins) {", "el.addEventListener('mouseenter'")
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

    def test_mobile_drawer_opens_on_the_left(self):
        css = _read('static/css/style.css')
        block = _slice(css, '@media (min-width: 601px) and (max-width: 900px) {', '\n}')
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
