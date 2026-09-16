"""The Logs page against the design that produced it.

DESIGN.md section 16 (approved 2026-08-03) settles this page; rollout is
dev/changelog/447. Every case here is a decision from that round, or a defect it
carried in, that a careless edit would quietly undo:

  * 16.1 settles the FIXED BOX: the log region scrolls and the page does not. The
    version this replaced sized the box with `calc(100vh - 310px)` - a hand-
    totalled offset that stops matching the moment .main grows an alert banner or
    a restart banner, both of which appear without warning. The height is taken by
    flex off the shell instead, so nothing has to know how tall the chrome is.
  * 16.1 also settles RULE + TINT on a WARNING-or-worse row. This is the one place
    3.4's app-wide no-status-tint rule is deliberately lifted, and section 16
    lifts it by name. The severity rides on the row's own `sev` class, never on a
    level class - `.log-lv.ERROR` is the level COLUMN's rule.
  * 16.5 item 4: the phone filter is a summary bar plus a sheet, and the sheet is
    buildModal() (which style.css already turns into a bottom sheet at that width,
    9.6). There is deliberately no second overlay component, and no second copy of
    the chips - the toolbar node is MOVED into the sheet and moved back.
  * 16.5 item 5: the SOURCE COLUMN SURVIVES at phone width. The page used to
    `display: none` it at 768px, which deletes the one field saying which
    subsystem is talking - the opposite of what this app is for.
  * 16.6: one writer per region. appendRow() is the only thing that inserts a node
    into the box; the filter only toggles `hidden` on rows already there. That is
    also what keeps a filter change from destroying a text selection mid-copy
    (dev/docs/BUGS.md 2026-07-18; the append half is guarded separately by
    tests/test_static_invariants.py::LiveLogRenderInvariants).
  * The part-2b carry-forward: a page-local <style> loads after style.css, so a
    re-declaration of a shared component silently wins for that page alone. The
    page used to restate the chip and the level colors in old rgba() literals.

Painted geometry - whether the box actually fills the viewport, the 375px stack,
the sheet's real height - needs layout and is browser work, not anything this file
can assert.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_logs_page_conformance
"""
import os
import re
import unittest
from unittest.mock import patch

from tests.support.app import make_test_app

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(REPO, 'templates', 'logs.html')
JS = os.path.join(REPO, 'static', 'js', 'logs.js')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _style_block(html):
    """The page's own <style> block, comments stripped.

    Stripped because the comments in it quote the rules they replaced - a scan for
    the old spelling otherwise matches the note explaining why it is gone.
    """
    m = re.search(r'<style>(.*?)</style>', html, re.S)
    assert m, 'templates/logs.html no longer carries a <style> block'
    return re.sub(r'/\*.*?\*/', '', m.group(1), flags=re.S)


def _media_block(css, query):
    """The body of `@media <query> { ... }`, matched by brace depth."""
    start = css.index(query)
    open_brace = css.index('{', start)
    depth = 0
    for m in re.finditer(r'[{}]', css[open_brace:]):
        depth += 1 if m.group(0) == '{' else -1
        if depth == 0:
            return css[open_brace:open_brace + m.end()]
    raise AssertionError(f'unbalanced braces after {query}')


def _fn_body(js, name):
    """Body of a `  function name(...)` declared at IIFE indent."""
    m = re.search(r'^  function\s+' + re.escape(name) + r'\s*\(', js, re.M)
    assert m, f'static/js/logs.js has no {name}()'
    end = re.compile(r'^  \}\s*$', re.M).search(js, m.end())
    return js[m.start():end.end()] if end else js[m.start():]


class LogsPageConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tpl = _read(TPL)
        cls.css = _style_block(cls.tpl)
        cls.js = _read(JS)
        # One app and one render for the whole class: every case reads the markup and
        # none rewrites the state it was rendered from (dev/changelog/979).
        cls.t = make_test_app()
        cls.client = cls.t.app.test_client()
        cls.html = cls.client.get('/logs').get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    # ── Page chrome ─────────────────────────────────────────────────────

    def test_the_page_is_converted(self):
        self.assertIn('class="page-head"', self.html)
        self.assertNotIn('class="page-header"', self.html)
        self.assertEqual(len(re.findall(r'<h1[ >]', self.html)), 1,
                         'DESIGN.md 3.10: exactly one h1 per page')

    def test_the_page_carries_no_inline_script(self):
        """The script moved to static/js/logs.js; an inline block means it came back."""
        for m in re.finditer(r'<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>', self.tpl, re.S):
            self.assertIn('src=', m.group('attrs'),
                          f'templates/logs.html carries inline script:\n{m.group("body")[:200]}')
        self.assertIn('js/logs.js', self.tpl)

    def test_the_page_declares_the_shell_body_class(self):
        """16.1's fixed box is a rule about the shell, so the page must reach it."""
        self.assertRegex(self.html, r'<body class="[^"]*\bon-logs\b')

    def test_the_status_badge_starts_at_the_nothing_active_state(self):
        """Server-rendered markup must equal the state before any JS has run."""
        m = re.search(r'<span class="badge ([^"]+)" id="log-status">([^<]+)</span>', self.html)
        self.assertIsNotNone(m, 'the stream status badge is no longer server-rendered')
        self.assertEqual(m.group(1), 'badge-scheduled')
        self.assertEqual(m.group(2).strip(), 'Connecting')
        self.assertIn("setStatus('Live'", self.js, 'nothing ever promotes the badge to Live')

    def test_the_page_names_the_file_it_tails(self):
        with patch('app.routes.logs._log_file_path', return_value='/var/log/channelbin.log'):
            html = self.client.get('/logs').get_data(as_text=True)
        self.assertIn('/var/log/channelbin.log', html)

    def test_a_missing_log_file_setting_does_not_break_the_head(self):
        with patch('app.routes.logs._log_file_path', return_value=None):
            resp = self.client.get('/logs')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('None', resp.get_data(as_text=True).split('</h1>')[0])

    def test_no_log_file_configured_says_so_rather_than_showing_an_empty_page(self):
        """`logging.file` has no default, so an install that never set one renders a page
        with no rows, nothing wrong and nothing said - which is the silence this app exists
        to remove. It cost a first-container install a full diagnosis (dev/changelog/981).
        The page must name the setting and say where the logs are actually going."""
        with patch('app.routes.logs._log_file_path', return_value=None):
            html = self.client.get('/logs').get_data(as_text=True)
        self.assertIn('empty-state', html)
        self.assertIn('logging.file', html)
        self.assertIn('docker logs', html)

    def test_a_configured_log_file_shows_no_such_notice(self):
        with patch('app.routes.logs._log_file_path', return_value='/var/log/channelbin.log'):
            html = self.client.get('/logs').get_data(as_text=True)
        self.assertNotIn('No log file is configured', html)

    # ── The fixed box (16.1) ────────────────────────────────────────────

    def test_the_box_is_not_sized_by_a_hand_totalled_offset(self):
        """`calc(100vh - Npx)` is the defect: .main can grow a banner at any time."""
        offenders = re.findall(r'calc\(\s*100d?vh\s*-\s*\d+px\s*\)', self.css)
        self.assertEqual(offenders, [], f'the log box is back on a hand-totalled offset: {offenders}')
        self.assertRegex(self.css, r'\.log-box\s*\{[^}]*flex:\s*1',
                         'the box no longer takes its height from flex')

    def test_the_shell_height_is_not_defeated_by_its_own_min_height(self):
        """style.css gives .shell `min-height: 100vh`, and min-height beats height.

        Caught in the browser pass: without `min-height: 0` the page's height rule was
        ignored at every width carrying the mobile top bar - .topnav is sticky rather
        than fixed, so it keeps its 52px above a shell that still insisted on a full
        100vh, and the page scrolled by exactly that much.
        """
        m = re.search(r'body\.on-logs \.shell\s*\{([^}]*)\}', self.css)
        self.assertIsNotNone(m, 'the page no longer sizes the shell')
        self.assertIn('min-height: 0', m.group(1))

    def test_the_shell_height_reads_the_nav_token(self):
        """A second hand-written copy of the top bar's height is the CSS defect class."""
        self.assertIn('var(--topnav-h)', self.css)
        self.assertNotIn('52px', self.css)

    # ── Severity (16.1) ─────────────────────────────────────────────────

    def test_severity_is_a_rule_and_a_tint_on_the_row(self):
        m = re.search(r'\.log-row\.sev\s*\{([^}]*)\}', self.css)
        self.assertIsNotNone(m, 'the severity row rule is gone')
        self.assertIn('border-left', m.group(1))
        self.assertIn('rgba(var(--bad-rgb)', m.group(1))
        warn = re.search(r'\.log-row\.sev\.warn\s*\{([^}]*)\}', self.css)
        self.assertIsNotNone(warn, 'WARNING rows no longer differ from ERROR rows')
        self.assertIn('rgba(var(--warn-rgb)', warn.group(1))

    def test_severity_does_not_ride_on_a_level_class(self):
        """`.log-lv.ERROR` is the level COLUMN's rule - on the row it repaints the row."""
        self.assertNotRegex(self.css, r'\.log-row\.(ERROR|CRITICAL|WARNING)\b')
        m = re.search(r'row\.className = `log-row\$\{([^`]*)\}`', self.js)
        self.assertIsNotNone(m, 'the row class is no longer built from the severity alone')
        self.assertNotIn('r.level', m.group(1).replace("r.level === 'ERROR'", '')
                         .replace("r.level === 'CRITICAL'", '').replace("r.level === 'WARNING'", ''))

    def test_the_page_uses_tokens_rather_than_palette_literals(self):
        """The old page hardcoded the palette; a literal cannot follow a token change."""
        literals = re.findall(r'rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*[,)]', self.css)
        self.assertEqual(literals, [], f'raw color literals are back in the page style: {literals}')

    # ── One writer per region (16.6) ────────────────────────────────────

    def test_the_filter_never_rebuilds_the_log_region(self):
        body = _fn_body(self.js, 'applyFilters')
        self.assertNotIn('innerHTML', body,
                         'the filter rebuilds the log list, so the stream and the filter '
                         'both own it (and a rebuild destroys a live text selection)')
        self.assertIn('.hidden = ', body)

    def test_rows_are_hidden_by_the_attribute_not_by_a_style(self):
        """style.css ships `[hidden] { display: none !important }` for exactly this."""
        self.assertNotIn('style.display', self.js)

    def test_the_count_is_recomputed_by_the_filter(self):
        """A count beside filterable rows must never keep its first total."""
        self.assertIn('syncCounts()', _fn_body(self.js, 'applyFilters'))
        counts = _fn_body(self.js, 'syncCounts')
        self.assertIn('shown', counts)
        self.assertIn('log-count', counts)

    # ── Shared components, not page copies ──────────────────────────────

    def test_the_page_does_not_restate_a_shared_component(self):
        """A page-local rule loads after style.css and silently wins for this page."""
        # A SCOPED use (`.log-chips .chip { padding }`) is the sanctioned way to
        # vary a shared component for one page. What is banned is re-declaring the
        # component itself - a selector that is nothing but the shared class.
        shared = ('chip', 'search', 'badge', 'btn', 'card', 'modal', 'modal-panel',
                  'page-head', 'empty-state')
        offenders = [name for name in shared
                     if re.search(r'^\s*\.' + re.escape(name) + r'\s*[,{]', self.css, re.M)]
        self.assertEqual(offenders, [],
                         f'templates/logs.html re-declares shared components: {offenders}')

    def test_the_filter_chips_are_the_shared_chip(self):
        self.assertRegex(self.js, r'class="chip')
        for path in (TPL, JS):
            text = _read(path)
            self.assertNotIn('source-chip', text)
            self.assertNotIn('level-chip', text)

    def test_the_search_box_carries_the_clear_button(self):
        """DESIGN.md 3.11 makes the clear x mandatory; this page shipped without it."""
        self.assertIn('class="search-wrap"', self.tpl)
        self.assertIn('class="search-clear"', self.tpl)
        self.assertIn("classList.toggle('has-text'", self.js)

    def test_the_phone_sheet_is_the_shared_modal(self):
        """9.6: buildModal already becomes a bottom sheet - no second overlay."""
        self.assertIn('buildModal({', self.js)
        for name in ('sheet-scrim', 'class="sheet"', '.sheet {'):
            self.assertNotIn(name, self.tpl + self.js,
                             f'a second overlay component ({name}) is back')

    def test_the_filter_controls_exist_exactly_once(self):
        """The sheet MOVES the toolbar; a second copy would be a second state."""
        for el_id in ('src-chips', 'lv-chips', 'log-search'):
            self.assertEqual(self.html.count(f'id="{el_id}"'), 1, el_id)
        self.assertIn('filterSlot.appendChild(filters)', self.js,
                      'the toolbar is never put back, so closing the sheet loses the filter')

    # ── Phone layout (16.5) ─────────────────────────────────────────────

    def test_the_source_column_survives_at_phone_width(self):
        phone = _media_block(self.css, '@media (max-width: 768px)')
        self.assertNotRegex(phone, r'\.log-src[^{]*\{[^}]*display:\s*none',
                            'hiding the source column deletes information rather than '
                            'rearranging it (DESIGN.md 16.5 item 5)')
        self.assertRegex(phone, r'\.log-msg\s*\{[^}]*grid-column:\s*1\s*/\s*-1',
                         'the row no longer goes two-line, so the message has no width')

    def test_the_row_grid_uses_no_content_dependent_track(self):
        """Each row is its own grid container, so an auto track sizes per row."""
        for m in re.finditer(r'\.log-row[^{]*\{[^}]*grid-template-columns:\s*([^;]+);', self.css):
            for track in m.group(1).split():
                self.assertNotIn(track.strip(), ('auto', 'max-content', 'min-content', 'fit-content'),
                                 f'content-dependent track in `{m.group(1)}` - the timestamp '
                                 'column would jitter line to line')

    def test_the_desktop_toolbar_slot_is_hidden_rather_than_the_toolbar(self):
        """A rule on .log-toolbar would also hide it inside the sheet."""
        shell = _media_block(self.css, '@media (max-width: 900px)')
        self.assertRegex(shell, r'#log-filter-slot\s*\{[^}]*display:\s*none')
        self.assertNotRegex(shell, r'\.log-toolbar\s*\{[^}]*display:\s*none')

    # ── The source vocabulary ───────────────────────────────────────────

    def test_three_noisy_sources_are_off_by_default_and_still_offered(self):
        """Off by default is not hidden: the chip renders either way."""
        decl = re.findall(r"\{ id: '([^']+)',\s*label: '[^']*',\s*hidden: (true|false)\s*\}", self.js)
        self.assertEqual(len(decl), 14, 'the source vocabulary changed size')
        self.assertEqual([i for i, h in decl if h == 'true'],
                         ['werkzeug', 'apscheduler.scheduler', 'apscheduler.executors.default'])
        self.assertIn('renderSourceChips', self.js)

    def test_an_undeclared_source_still_gets_a_chip(self):
        """A logger nobody listed must not be silently unfilterable."""
        self.assertIn('function ensureSource', self.js)
        self.assertIn('ensureSource(r.source)', _fn_body(self.js, 'appendRow'))


if __name__ == '__main__':
    unittest.main()
