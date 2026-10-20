"""Tier 0 - the CSS layer holds its consolidated spellings (dev/changelog/1245).

Each check below guards a consolidation the frontend passes of 2026-08-15 and 2026-09-15
found written several times over, and that one pass then folded onto one token, one class
or one rule. A restatement is not a style nit: the copies had already drifted apart in
rendering (a notice's border was a different green from its own background; a filled chip
was near-black on one page and white on the next), and a copy is what the next drift
starts from. None of this can be seen by the suite's other CSS scans, which ask whether a
token exists, not whether something re-typed its value.

  python3 -m unittest tests.test_css_token_restatements
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS_DIR = os.path.join(REPO, 'static', 'css')
JS_DIR = os.path.join(REPO, 'static', 'js')
TPL_DIR = os.path.join(REPO, 'templates')
APP_DIR = os.path.join(REPO, 'app')
STYLE = os.path.join(CSS_DIR, 'style.css')


def _walk(root, ext):
    for dirpath, _dirs, files in os.walk(root):
        for f in sorted(files):
            if f.endswith(ext):
                yield os.path.join(dirpath, f)


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _rel(path):
    return os.path.relpath(path, REPO)


def _css_sources():
    """The .css files and the <style> blocks inside templates - what the palette reaches."""
    for p in _walk(CSS_DIR, '.css'):
        yield p, _read(p)
    for p in _walk(TPL_DIR, '.html'):
        yield p, _read(p)


def _root_block(style_text):
    m = re.search(r':root\s*\{(.*?)\n\}', style_text, re.S)
    assert m, 'style.css must open its token block with :root {'
    return m.group(1)


def _hits(needle, exclude_root=False):
    out = []
    for path, text in _css_sources():
        hay = text
        if exclude_root and path == STYLE:
            hay = text.replace(_root_block(text), '')
        for i, line in enumerate(hay.splitlines(), 1):
            if needle in line:
                out.append(f'{_rel(path)}:{i}: {line.strip()[:90]}')
    return out


class StatusTokenRestatementTests(unittest.TestCase):
    """Values the palette now names, spelled as literals again somewhere else."""

    # literal -> the token that holds it. The literal must appear only inside style.css's
    # :root block, where the token is defined.
    TOKEN_VALUES = {
        '#8fd9a1': '--ok-text',
        '#f0a5a3': '--bad-text',
        '#e8c98a': '--warn-text',
        '188,140,255': '--purple-rgb / --purple-soft',
        '0 -4px 16px rgba(0,0,0,.35)': '--shadow-bar',
    }

    def test_a_token_value_is_written_only_where_the_token_is_defined(self):
        for literal, token in self.TOKEN_VALUES.items():
            with self.subTest(literal=literal):
                self.assertEqual(
                    _hits(literal, exclude_root=True), [],
                    f'{literal!r} is {token}; read the token instead of restating it')

    # Spellings that were never on the palette and were replaced outright: the pre-graphite
    # ok and accent values a notice border carried, the four hand-picked dark inks on a
    # status fill (--accent-contrast is the one dark ink on any fill), the search page's
    # own light red, and the pre-graphite border the group modal used for a missing color.
    RETIRED_LITERALS = (
        'rgba(63,185,80', 'rgba(88,166,255',
        '#2a0b0a', '#0b1220', '#f4938f', '#30363d',
    )

    def test_retired_hand_picked_values_do_not_come_back(self):
        for literal in self.RETIRED_LITERALS:
            with self.subTest(literal=literal):
                self.assertEqual(_hits(literal), [], f'{literal!r} was retired for a token')

    def test_no_white_or_black_ink_on_a_filled_control(self):
        """`color: #fff` / `color: #000` beside a status or accent fill is the two-polarity
        defect: a count badge drew white on blue next to a button drawing dark on blue."""
        offenders = []
        for path, text in _css_sources():
            for i, line in enumerate(text.splitlines(), 1):
                if re.search(r'background:\s*var\(--(accent|ok|warn|bad|live)\)', line) and \
                        re.search(r'color:\s*#(fff|000)\b', line):
                    offenders.append(f'{_rel(path)}:{i}')
        self.assertEqual(offenders, [], 'ink on a filled control is var(--accent-contrast)')


class StatusEdgeTokenTests(unittest.TestCase):
    """The 3px status edge (dev/changelog/878) reads --edge-w at every bar and every
    reservation, so widening it is one edit rather than sixteen coupled literals."""

    _BAR_RE = re.compile(r'::before\s*\{[^}]*left: 0; top: 0; bottom: 0; width: ([^;]+);')

    def test_every_inset_status_bar_reads_the_token(self):
        text = _read(STYLE)
        widths = self._BAR_RE.findall(text)
        self.assertGreaterEqual(len(widths), 7, 'the seven status-edge hosts should still be here')
        self.assertEqual(set(widths), {'var(--edge-w)'}, widths)

    def test_no_reservation_hand_sums_the_edge_back_in(self):
        """The hosts' left paddings were 19/16/15/14/13px literals; each is now
        calc(<room> + var(--edge-w)). A bare left pad on one of these hosts is a copy that
        will stop matching the bar the moment the token moves."""
        text = re.sub(r'/\*.*?\*/', '', _read(STYLE), flags=re.S)
        hosts = ('.rows .row', '.dash-rows .drow', '.acct-rows .arow', '.grp-item',
                 '.gm-card', '.live-strip', '.mtile')
        offenders = []
        for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', text):
            prelude, body = m.group(1).strip(), m.group(2)
            if not any(prelude.endswith(h) or prelude == h for h in hosts):
                continue
            pads = re.findall(r'padding(?:-left)?:\s*([^;]+);', body)
            for pad in pads:
                left = pad.split()[-1]
                if left.endswith('px') and 'edge-w' not in pad:
                    offenders.append(f'`{prelude}` {pad}')
        self.assertEqual(offenders, [], 'write the left pad as calc(<room> + var(--edge-w))')


class SharedRecipeTests(unittest.TestCase):
    """Rules the CSS layer now writes once."""

    def test_the_text_input_focus_rule_has_one_spelling(self):
        """The border + focus recipe for a text input or select had been written ten times
        over before it was grouped above .form-control. A new copy here is the "5th copy"
        moment CLAUDE.md names - the control joins the grouped list instead."""
        hits = []
        for path, text in _css_sources():
            for i, line in enumerate(text.splitlines(), 1):
                if 'outline: none; border-color: var(--accent)' in line:
                    hits.append(f'{_rel(path)}:{i}')
        self.assertEqual(len(hits), 1, f'the focus recipe is written once, in style.css: {hits}')
        self.assertTrue(hits[0].startswith('static/css/style.css:'), hits)

    def test_page_intros_use_the_class_not_an_inline_size(self):
        """Eleven pages had `<p class="text-muted" style="font-size:13px">` - the same recipe
        at a size that is not on the type scale. It is .page-intro now."""
        hits = []
        for p in _walk(TPL_DIR, '.html'):
            for i, line in enumerate(_read(p).splitlines(), 1):
                if re.search(r'style="[^"]*font-size:\s*13px', line):
                    hits.append(f'{_rel(p)}:{i}')
        self.assertEqual(hits, [], 'use .page-intro (or a --fs-* token) instead')

    def test_severity_chips_tint_through_the_shared_tone_rule(self):
        """Logs' level chips and Notifications' routing chips each hand-wrote the same
        INFO/WARN/ERROR tint mapping. One rule in style.css, keyed by util.js's
        severityToneClass(), serves both; a page writing `.chip...active[data-...]` tints
        is the third copy."""
        offenders = []
        for p in _walk(TPL_DIR, '.html'):
            for i, line in enumerate(_read(p).splitlines(), 1):
                if re.search(r'\.chip[.\w-]*\.active\[data-(level|sev)=', line):
                    offenders.append(f'{_rel(p)}:{i}')
        self.assertEqual(offenders, [], 'tint severity chips through .chip.active.tone-*')
        style = _read(STYLE)
        for cls in ('.chip.active.tone-warn', '.chip.active.tone-bad', '.chip.active.tone-muted'):
            self.assertIn(cls, style)
        util = _read(os.path.join(JS_DIR, 'util.js'))
        for level in ('WARN', 'WARNING', 'ERROR', 'CRIT', 'CRITICAL', 'DEBUG'):
            self.assertRegex(util, rf"\b{level}: 'tone-", f'severityToneClass must map {level}')

    def test_the_phone_picker_bar_is_one_component(self):
        """Logs' filter bar and Settings' section picker were the same "labeled current
        value" button written twice, at two heights. Both are style.css's .pickbar."""
        for tpl in ('logs.html', 'settings.html'):
            text = _read(os.path.join(TPL_DIR, tpl))
            self.assertIn('class="pickbar"', text, tpl)
            self.assertNotRegex(text, r'\.(logfilter-btn|secpick-btn)\s*\{', tpl)
            self.assertNotIn('--pick-h:', text, f'{tpl} redefines the shared height')
        self.assertIn('--pick-h: 44px', _root_block(_read(STYLE)))

    def test_the_guide_header_height_is_one_value(self):
        """.guide-day-header's height, the segment label's height/line-height and the time
        header's sticky `top` were three independent 28px literals that must move together."""
        text = _read(os.path.join(CSS_DIR, 'guide.css'))
        self.assertIn('--guide-dayh: 28px', text)
        for sel, prop in (('.guide-day-header', 'height'), ('.guide-day-segment-label', 'height'),
                          ('.guide-day-segment-label', 'line-height'), ('.guide-time-header', 'top')):
            m = re.search(re.escape(sel) + r'\s*\{([^}]*)\}', text)
            self.assertIsNotNone(m, sel)
            self.assertRegex(m.group(1), rf'\b{prop}: var\(--guide-dayh\)', f'{sel} {prop}')

    def test_the_switch_component_has_a_disabled_state(self):
        """A disabled .switch rendered identically to a live one - same colors, same pointer
        cursor - which is the dead-knob class DESIGN.md 11.2 names. The states live on the
        component, never per page (dev/changelog/739 is the page-local rule that went wrong)."""
        style = _read(STYLE)
        self.assertIn('.switch input:disabled + .knob', style)
        self.assertIn('.switch input:checked:disabled + .knob', style)
        self.assertRegex(style, r'\.switch input:disabled[^{]*\{[^}]*cursor: not-allowed')


class RetiredCopyTests(unittest.TestCase):
    """Words and values that were retired and must not ship again."""

    def test_unschedule_is_not_user_facing_copy(self):
        """`Unschedule` was retired as a verb on 2026-08-26 (dev/changelog/814); the group
        page's kebab and a recording 409 message still shipped it. The lowercase route and
        function names (`unschedule_recording`, the `/unschedule` endpoint) are identifiers,
        not copy, and are not what this scans for."""
        hits = []
        for group, ext in ((TPL_DIR, '.html'), (JS_DIR, '.js'), (APP_DIR, '.py')):
            for p in _walk(group, ext):
                for i, line in enumerate(_read(p).splitlines(), 1):
                    if 'Unschedule' in line:
                        hits.append(f'{_rel(p)}:{i}')
        self.assertEqual(hits, [], 'say "Remove schedule" (a check) or "Cancel" (a recording)')

    def test_the_default_color_has_one_home(self):
        """`#58a6ff` was typed as a default in eight places across three languages. It is
        app/ui_constants.py DEFAULT_COLOR, read by the models and routes and handed to the
        two modals by their pages."""
        hits = []
        for group, ext in ((APP_DIR, '.py'), (JS_DIR, '.js'), (TPL_DIR, '.html')):
            for p in _walk(group, ext):
                if p.endswith(os.path.join('app', 'ui_constants.py')):
                    continue
                for i, line in enumerate(_read(p).splitlines(), 1):
                    if '58a6ff' in line:
                        hits.append(f'{_rel(p)}:{i}')
        self.assertEqual(hits, [], 'read DEFAULT_COLOR instead')

    def test_title_case_is_gone_from_the_buttons_the_pass_named(self):
        """DESIGN.md 4: sentence case on every button, with the search page's two grain
        pills the only exception. These are the labels that still carried a capitalized
        second word."""
        sources = {_rel(p): _read(p) for grp, ext in ((TPL_DIR, '.html'), (JS_DIR, '.js'))
                   for p in _walk(grp, ext)}
        for label in ('>Browse Channels<', '>Select All<', '>Deselect All<', '>Force Cancel<',
                      '>Proceed Anyway<', "'Schedule Recording'"):
            with self.subTest(label=label):
                self.assertEqual([f for f, t in sources.items() if label in t], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
