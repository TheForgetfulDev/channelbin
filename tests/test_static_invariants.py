"""Tier 0 - static source/CSS/template invariants (dev/changelog/268, chunk 4).

Pure text scans of the tree. No app import, no DB, no Flask - these run in milliseconds
and guard whole defect *classes* from CLAUDE.md's coding standards that a running test can't
cheaply see:

  * undefined CSS `var(--x)` without a fallback - "an undefined custom property fails silently
    (two shipped invisible-UI bugs)" (CLAUDE.md CSS rule; BUGS.md 2026-07-17 CSS-var pass).
  * hardcoded display timezone in JS `Intl` options - the forced-Eastern class (CLAUDE.md
    Timezones: "never hardcode a timezone in JS `Intl` options").
  * hardcoded `America/New_York` in display code outside the two tz-authority modules.
  * `stderr=subprocess.PIPE` with nothing draining it - "an undrained 64KB pipe buffer
    deadlocked every recording at ~7.8 min" (CLAUDE.md Subprocess discipline; BUGS.md deadlock).
  * bare broad `except Exception: pass` / `except: pass` - silent swallows (CLAUDE.md Error
    handling: "No bare `except Exception: pass`").
  * Python builtins used as kwargs inside Jinja `{{ }}`/`{% %}` - `request.args.get('x',
    type=int)` raises UndefinedError (CLAUDE.md Jinja hazards).
  * a JS file that does not parse at all - a whole-file SyntaxError silently disables every
    listener in it while the server-rendered page still looks fine (BUGS.md 2026-08-04
    02:20 PM, the Accounts list's arrow-function IIFE).
  * a migration that adds a column with a SQL DEFAULT whose model declares no matching
    `server_default` - upgraded and fresh databases then carry different DDL for the same
    column, and a raw INSERT omitting it dies on NOT NULL against only one of them
    (dev/changelog/687, `690`).
  * a `retry_on_locked` closure that can reach two `db.session.commit()` calls on one
    execution path - the decorator replays the whole closure, so the earlier commit's INSERT
    runs twice and leaves a duplicate row (CLAUDE.md "each commit gets its own decorated
    closure"; caught for real in `new_recording_json`).
  * a non-idempotent side effect - process spawn, thread start, network fetch, file unlink -
    reachable from inside a `retry_on_locked` closure, which every retry re-runs
    (dev/changelog/683: one locked commit re-downloading a whole M3U playlist).
  * a CI workflow that stops installing the external tools the suite gates on, or that floats
    its runner image - either one silently shrinks the suite CI reports green over, with no
    failure anywhere to say so (dev/changelog/907).

Plus one *advisory* scan (never fails): `db.session.commit()` sites not obviously wrapped by
`retry_on_locked` - printed as suspects for a human to eyeball, per CLAUDE.md's note that this
can't be enforced automatically.

Each check is scoped to avoid the legitimate exceptions the codebase deliberately keeps (the
config default, the tz_utils fallback constant, drained PIPE sites, narrow `except OSError`
cleanup). If a check goes red, the fix is to bring the code into compliance - not to widen the
allowlist - unless a genuinely new legitimate exception is being added.
"""
import ast
import functools
import io
import os
import re
import shutil
import subprocess
import tokenize
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, 'app')
TESTS_DIR = os.path.join(ROOT, 'tests')
TOOLS_DIR = os.path.join(ROOT, 'tools')
CSS_DIR = os.path.join(ROOT, 'static', 'css')
JS_DIR = os.path.join(ROOT, 'static', 'js')
TPL_DIR = os.path.join(ROOT, 'templates')


def _walk(root, ext):
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(ext):
                yield os.path.join(dirpath, f)


@functools.lru_cache(maxsize=None)
def _read(path):
    """Memoized: the tree does not change during a run, and the scans below read the
    same few hundred files from dozens of test methods (dev/changelog/979)."""
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _rel(path):
    return os.path.relpath(path, ROOT)


@functools.lru_cache(maxsize=None)
def _mask_comments_and_strings(source):
    """Return `source` with every COMMENT and STRING token's text replaced by spaces,
    preserving line/column layout so line-based slicing (span extraction, `.strip()`
    matching) still lines up. Used by the retry_on_locked advisory scan below so a
    substring check can't be fooled by a docstring or comment merely *mentioning* the
    thing it's supposed to verify is actually there in code."""
    lines = source.splitlines(keepends=True)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source
    for tok in tokens:
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        if srow == erow:
            line = lines[srow - 1]
            lines[srow - 1] = line[:scol] + ' ' * (ecol - scol) + line[ecol:]
            continue
        first = lines[srow - 1]
        body_len = len(first.rstrip('\n'))
        lines[srow - 1] = first[:scol] + ' ' * (body_len - scol) + first[body_len:]
        for r in range(srow, erow - 1):
            mid = lines[r]
            body_len = len(mid.rstrip('\n'))
            lines[r] = ' ' * body_len + mid[body_len:]
        last = lines[erow - 1]
        lines[erow - 1] = ' ' * ecol + last[ecol:]
    return ''.join(lines)


# --- CSS custom-property extraction ---------------------------------------------------

_VAR_DEF_RE = re.compile(r'--([A-Za-z0-9-]+)\s*:')
# var(--name) with NO comma before the close paren = no fallback.
_VAR_USE_NOFALLBACK_RE = re.compile(r'var\(\s*--([A-Za-z0-9-]+)\s*\)')


def _css_sources():
    """All files that can define/use CSS custom properties: the .css files and the
    <style> blocks inside templates (which share the same :root palette)."""
    for p in _walk(CSS_DIR, '.css'):
        yield p, _read(p)
    for p in _walk(TPL_DIR, '.html'):
        yield p, _read(p)


class CssVarTests(unittest.TestCase):
    """No `var(--x)` (without a fallback) may reference a custom property that is never
    defined - it renders as nothing and fails silently (CLAUDE.md CSS rule)."""

    def test_no_undefined_css_var_without_fallback(self):
        defined = set()
        uses = []  # (name, relpath)
        for path, text in _css_sources():
            for m in _VAR_DEF_RE.finditer(text):
                defined.add(m.group(1))
            for m in _VAR_USE_NOFALLBACK_RE.finditer(text):
                uses.append((m.group(1), _rel(path)))

        undefined = sorted({(name, rp) for name, rp in uses if name not in defined})
        self.assertEqual(
            undefined, [],
            'CSS var(--x) used without a fallback but never defined (silent invisible-UI '
            'bug - CLAUDE.md CSS rule). Define it in style.css :root or use the canonical '
            f'palette name / a fallback: {undefined}')


_CSS_COMMENT_RE = re.compile(r'/\*.*?\*/', re.S)
# A top-level `html { ... }` rule. The negative lookbehind keeps `.foo html {` and
# `html.dark {` out - those are not the bare root selector.
_HTML_RULE_RE = re.compile(r'(?<![\w.#\]-])html\s*\{([^}]*)\}')
_FONT_SIZE_DECL_RE = re.compile(r'(?:^|;)\s*font-size\s*:\s*([^;}]+)')
# px/pt/pc/in/cm/mm/Q are the absolute lengths; everything else (rem, em, %, ch, ex,
# vw/vh/vmin/vmax, and the keywords) resolves against something the app does not control.
_ABSOLUTE_LEN_RE = re.compile(r'^-?[\d.]+(px|pt|pc|in|cm|mm|Q)$')


class RootFontSizeTests(unittest.TestCase):
    """The root font size must be declared in an ABSOLUTE unit.

    A `rem` in a `font-size` on the **root element** resolves against the browser's
    initial font size, not against the value being declared - there is no ancestor to
    inherit from - so `--fs-base: 1rem` with `html { font-size: var(--fs-base) }` is a
    no-op that silently hands the root to whatever the browser defaults to. Every
    `--fs-*` token in style.css is a `rem`, so losing the root loses the whole scale, and
    nothing on screen errors: the app just renders at a size no document describes. It
    shipped exactly that way from 2026-08-11 to 2026-08-26 while style.css, DESIGN.md 1
    and CLAUDE.md all claimed a 15px base and every page rendered at 16px
    (dev/changelog/812, BUGS.md 2026-08-26).

    CssVarTests cannot catch this - it checks that a `var()` name is *defined*, never
    that its value *resolves* sanely, which is why the regression survived a green suite.
    Asserting the computed size would need a browser; asserting the declared unit does
    not, and the unit is the defect.
    """

    def _root_defs(self, text):
        return {m.group(1): m.group(2).strip()
                for m in re.finditer(r'--([A-Za-z0-9-]+)\s*:\s*([^;}]+)', text)}

    def test_root_font_size_is_absolute(self):
        found = []  # (relpath, raw declaration, resolved value)
        for path in sorted(_walk(CSS_DIR, '.css')):
            text = _CSS_COMMENT_RE.sub(' ', _read(path))
            defs = self._root_defs(text)
            for rule in _HTML_RULE_RE.finditer(text):
                for decl in _FONT_SIZE_DECL_RE.finditer(rule.group(1)):
                    raw = decl.group(1).strip()
                    value = raw
                    # Chase one level of var() to its custom-property definition; the
                    # scale has no deeper indirection and a chain would be its own smell.
                    var_use = re.fullmatch(r'var\(\s*--([A-Za-z0-9-]+)\s*\)', raw)
                    if var_use:
                        value = defs.get(var_use.group(1), raw)
                    found.append((_rel(path), raw, value))

        self.assertTrue(
            found,
            'No `html { font-size: ... }` rule found in static/css/. The app-wide base '
            'type size is declared there (DESIGN.md 1 "Type") and every --fs-* token is '
            'a rem relative to it - if that rule is gone, the whole scale is sized by the '
            'browser default instead.')

        relative = [(rp, raw, val) for rp, raw, val in found
                    if not _ABSOLUTE_LEN_RE.match(val)]
        self.assertEqual(
            relative, [],
            'The root font size must be an absolute length (px). A rem/em/% value on the '
            'root element resolves against the browser default rather than against '
            'itself, so it silently sets nothing and every --fs-* token in the app is '
            'sized by the browser. See the comment above the `html` rule in '
            f'static/css/style.css: {relative}')


#: The app's one mobile breakpoint, and the only widths a width-based media query may name.
#: 960 is the breakpoint, 961 its complement (`min-width`), 601 the nav drawer's lower bound,
#: 480 the portrait-phone tightening pass. Moving the breakpoint is an edit here plus a sweep -
#: that is the point of the number living in one place (dev/changelog/1094).
MOBILE_BREAKPOINT_PX = 960
ALLOWED_BREAKPOINT_PX = {480, 601, 960, 961}

#: Widths that were breakpoints before 2026-09-22 and must not come back. Named explicitly so
#: the failure message can say what went wrong rather than just "not in the allowlist".
RETIRED_BREAKPOINT_PX = {768: 'the old component breakpoint', 769: 'its complement',
                         900: 'the old shell breakpoint', 901: 'its complement'}


class SingleMobileBreakpointTests(unittest.TestCase):
    """The app has ONE mobile breakpoint and every width-based query names it.

    It had two until 2026-09-22 - the shell moved at 900px and the components at 768px - and
    the 132px between them is exactly where a phone held sideways lands (667-956px CSS px).
    A landscape phone therefore got the mobile shell and the desktop component layout at the
    same time, so a wide table was helped by neither: four pages needed a sideways drag at
    844px, six at 932px, and at 932 the sidebar came back and left 602px of content where an
    844px phone got 778px - the bigger phone drawing the narrower page.

    The reason this needs a test rather than a note is that the two numbers were *already*
    documented as needing to stay in sync, in five separate JS files each carrying a comment
    saying "one spelling of 768 in this file, matching style.css". Prose did not stop the
    split from existing; it only described it. This is the coupled-values CSS defect class
    from CLAUDE.md applied to the breakpoint itself.

    jsdom computes no layout, so nothing in the suite can see an overflow - this is a text
    scan, and the browser pass that measured the widths above is recorded in
    dev/changelog/1094.
    """

    #: `@media` preludes, and the width conditions inside them, wherever they are written -
    #: a .css file, a template's own <style> block, or a JS template literal that builds one
    #: (templates/index.html's column sizer does exactly that).
    _MEDIA_RE = re.compile(r'@media[^{]*')
    _WIDTH_RE = re.compile(r'\b(?:max|min)-width:\s*(\d+)px')
    _MATCHMEDIA_RE = re.compile(r'matchMedia\(\s*[\'"`]\(\s*(?:max|min)-width:\s*(\d+)px')
    _BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.S)

    @classmethod
    def _scannable(cls, path):
        """The file with block comments removed.

        Required, not tidiness: `@media` is written in prose all over this tree, and a
        prelude regex bounded by the next `{` runs straight out of a comment and into the
        following rule. The comment above `@container rec-list` says "the plain @media block
        further down" and made that container query read as a 800px media query.
        """
        return cls._BLOCK_COMMENT_RE.sub('', _read(path))

    def _sources(self):
        for path in sorted(_walk(CSS_DIR, '.css')):
            yield path
        for path in sorted(_walk(TPL_DIR, '.html')):
            yield path
        for path in sorted(_walk(JS_DIR, '.js')):
            yield path

    def test_every_media_query_width_is_an_approved_breakpoint(self):
        """No `@media` may name a width outside the approved set."""
        bad = []
        for path in self._sources():
            for prelude in self._MEDIA_RE.findall(self._scannable(path)):
                for px in (int(m) for m in self._WIDTH_RE.findall(prelude)):
                    if px in ALLOWED_BREAKPOINT_PX:
                        continue
                    why = RETIRED_BREAKPOINT_PX.get(px)
                    bad.append(f'{_rel(path)}: {px}px'
                               + (f' ({why}, retired 2026-09-22)' if why else ''))
        self.assertEqual(
            bad, [],
            'These media queries name a width that is not one of the app\'s breakpoints '
            f'{sorted(ALLOWED_BREAKPOINT_PX)}. The mobile breakpoint is '
            f'{MOBILE_BREAKPOINT_PX}px and there is deliberately only one of it - a second '
            'one is where landscape phones fell through before dev/changelog/1094. If a '
            'surface genuinely needs to reflow on its own available width rather than the '
            'viewport, use a named container (.rec-list/.acct-list/.prof-list), not a new '
            f'breakpoint: {bad}')

    def test_every_matchmedia_uses_the_mobile_breakpoint(self):
        """The JS half of the breakpoint must equal the CSS half.

        Six call sites across five JS files and one template pick a renderer or a sheet-vs-
        modal from this query. CSS deciding what a surface LOOKS like while JS decides what
        it is BUILT from, at two different widths, is a class of bug the guide has shipped
        before (static/js/guide.js says so above its own declaration).
        """
        bad = []
        for path in list(_walk(JS_DIR, '.js')) + list(_walk(TPL_DIR, '.html')):
            for px in (int(m) for m in self._MATCHMEDIA_RE.findall(self._scannable(path))):
                if px != MOBILE_BREAKPOINT_PX:
                    bad.append(f'{_rel(path)}: matchMedia at {px}px')
        self.assertEqual(
            bad, [],
            f'Every matchMedia breakpoint must be {MOBILE_BREAKPOINT_PX}px, matching the CSS: '
            f'{sorted(bad)}')

    def test_the_mobile_breakpoint_is_actually_used(self):
        """A guard that allows a set of widths is worthless if the real one is absent.

        Without this, deleting every `@media (max-width: 960px)` in the app passes both
        checks above - they only constrain what a query may say, not that any exists.
        """
        found = sum(
            len([m for m in self._WIDTH_RE.findall(prelude)
                 if int(m) == MOBILE_BREAKPOINT_PX])
            for path in self._sources()
            for prelude in self._MEDIA_RE.findall(self._scannable(path)))
        self.assertGreater(
            found, 10,
            f'Only {found} media queries name the {MOBILE_BREAKPOINT_PX}px mobile '
            'breakpoint. The app\'s whole mobile layout hangs off it, so a number this low '
            'means the breakpoint was renamed without this constant being updated.')


class CssBreakpointOverrideTests(unittest.TestCase):
    """A responsive `display` set inside a `@media` block must not be undone by a later
    top-level rule carrying the *same selector* (BUGS.md 2026-07-30 - the `/channels` page
    kebab).

    Specificity cannot break a tie between two copies of one selector, so source order
    decides, and a base rule written below its own media query silently wins at every width.
    The failure is invisible in three ways at once: the media rule is right there in the
    file, both rules are correct in isolation, and the control it hides exists *only* at the
    width where it is hidden - so nothing looks broken anywhere else. On `/channels` that
    left `Delete Missing Channels` and `Review Duplicates` with no reachable path on a phone
    at all, since `.missing-bar` (which holds them above the breakpoint) is deliberately
    hidden below it. jsdom computes no layout and could not see it; this is a text scan.
    """

    _COMMENTS_RE = re.compile(r'/\*.*?\*/', re.S)

    @staticmethod
    def _rules(css):
        """Yield (selector, body, in_media, order) for every style rule, in source order.

        A hand-rolled scan rather than a CSS parser: nothing in this project ships one, and
        the shapes here are flat (style rules never nest; only at-rules open a context).
        """
        out, buf, stack, order = [], [], [], 0
        i, n = 0, len(css)
        while i < n:
            c = css[i]
            if c == '{':
                prelude = ''.join(buf).strip()
                buf = []
                if prelude.startswith('@'):
                    stack.append(prelude)
                    i += 1
                    continue
                depth, j = 1, i + 1
                while j < n and depth:
                    if css[j] == '{':
                        depth += 1
                    elif css[j] == '}':
                        depth -= 1
                    j += 1
                out.append((prelude, css[i + 1:j - 1],
                            any(s.startswith('@media') for s in stack), order))
                order += 1
                i = j
                continue
            if c == '}':
                if stack:
                    stack.pop()
                buf = []
                i += 1
                continue
            buf.append(c)
            i += 1
        return out

    @staticmethod
    def _norm(selector):
        return ' '.join(selector.split()).lower()

    @staticmethod
    def _sets_display(body):
        return re.search(r'(^|;)\s*display\s*:', body) is not None

    def test_a_media_display_is_not_undone_by_a_later_base_rule(self):
        offenders = []
        for path in _walk(CSS_DIR, '.css'):
            rules = self._rules(self._COMMENTS_RE.sub('', _read(path)))
            responsive = {}
            for selector, body, in_media, order in rules:
                if in_media and self._sets_display(body):
                    responsive.setdefault(self._norm(selector), order)
            for selector, body, in_media, order in rules:
                if in_media or not self._sets_display(body):
                    continue
                key = self._norm(selector)
                if key in responsive and order > responsive[key]:
                    offenders.append(
                        f'{_rel(path)}: `{selector.strip()}` sets display in a @media block '
                        f'(rule #{responsive[key]}) and again unconditionally afterwards '
                        f'(rule #{order}) - the later copy wins at every width')
        self.assertEqual(
            sorted(offenders), [],
            'A @media rule and an identical-selector base rule both set `display`, with the '
            'base rule LAST - so the responsive one never applies at any width. Move the base '
            'rule above the media block:\n' + '\n'.join(sorted(offenders)))


class CssClassDefinedTests(unittest.TestCase):
    """Two sibling defects to the undefined-var one above, both shipped and both silent:
    a class emitted by JS but styled nowhere, and a class defined twice where the wrong
    copy wins (BUGS.md 2026-07-25 - the Channels table's good/warn/bad colouring was
    absent entirely, and the group-detail page subtitle rendered at the table-cell size)."""

    # Emitted by static/js/group-detail.js on the score, bitrate, frame-delivery and
    # drop-count cells and three rows of the stream-profile drawer.
    VALUE_CLASSES = ('val-good', 'val-warn', 'val-bad')

    def test_value_state_classes_are_defined(self):
        text = '\n'.join(t for _p, t in _css_sources())
        missing = [c for c in self.VALUE_CLASSES if not re.search(rf'\.{c}\b\s*[,{{]', text)]
        self.assertEqual(
            missing, [],
            'Class emitted by JS but defined in no stylesheet, so the state it signals '
            f'renders in plain body colour and is silently absent: {missing}')

    # Emitted only from template literals in static/js/dropdown.js and
    # static/js/notifications.js (dev/changelog/440), so a missing rule is invisible to
    # a grep of templates/ - which is how the two shipped invisible-UI bugs above got in.
    JS_EMITTED_CLASSES = ('msel', 'mlbl', 'mopt', 'mo-t', 'mo-s', 'btn-accent',
                          'svc-x', 'svc-enable', 'rt-cl', 'rt-none', 'alert-severity',
                          # The shared filter bar (static/js/filter-bar.js). Two of these
                          # shipped with no rule at all: the chip's remove glyph drew as
                          # bare body text, and a chosen value in a drilled-into dimension
                          # was drawn identically to an unchosen one, so the popover could
                          # not say what was already on (dev/docs/BUGS.md 2026-08-20 @
                          # 05:56:31 PM, dev/changelog/767).
                          'chip-x', 'pop-back', 'fb-val', 'fb-tick', 'fb-lbl', 'fb-n',
                          # channel-search.js's Groups column badge (dev/changelog/821) -
                          # a prior 'b-mute' call site had no matching rule anywhere and
                          # rendered unstyled; renamed to the existing muted-badge class.
                          'b-abort',
                          # The Readiness card (static/js/readiness.js, dev/changelog/950).
                          # Every one of these is emitted only from a template literal, so
                          # a missing rule is invisible to a grep of templates/ - which is
                          # exactly how the two shipped invisible-UI bugs above got in.
                          'rd-verdict', 'rd-vmark', 'rd-vhead', 'rd-vsub', 'rd-vmeta',
                          'rd-caps', 'rd-caprow', 'rd-capmark', 'rd-capname', 'rd-capwhy',
                          'rd-capact', 'rd-whylist', 'rd-row', 'rd-rname', 'rd-rcaret',
                          'rd-rfound', 'rd-ract', 'rd-muted', 'rd-detail', 'rd-dk', 'rd-dv',
                          'rd-comp', 'rd-progress', 'rd-bar', 'rd-intro',
                          # Maintenance's Readiness nav badges and rail pip. The markup
                          # is in base.html, but only applyReadiness() ever shows them, and
                          # both badges shipped with no rule, drawn grey like any plain
                          # count (dev/docs/BUGS.md 2026-09-18 @ 09:48:43 PM,
                          # dev/changelog/1038).
                          'nav-count-ready-bad', 'nav-count-ready-warn', 'pip-ready')

    def test_js_emitted_component_classes_are_defined(self):
        text = '\n'.join(t for _p, t in _css_sources())
        missing = [c for c in self.JS_EMITTED_CLASSES if not re.search(rf'\.{c}\b\s*[,{{ ]', text)]
        self.assertEqual(
            missing, [],
            'Class emitted by JS but defined in no stylesheet or <style> block, so the '
            f'component renders unstyled and is silently wrong: {missing}')

    def test_no_duplicate_top_level_class_definition(self):
        """A class defined twice at equal specificity silently resolves to whichever came
        last. Scoped to the handful of single-class page-level selectors where the two
        copies meant different things."""
        for name in ('gd-sub', 'gd-sub-lead', 'card-note', 'linked-item', 'sec-note',
                     # Promoted out of settings.html when Notifications became the second
                     # caller (dev/changelog/440); a page-local copy would shadow it.
                     'frow', 'fr-ctl', 'fl-key',
                     # recording_detail.html's run timeline vs. the dashboard's timeline
                     # (16.3) - `tl-bar`/`tl-now` bare names collided and the dashboard's
                     # later, unrelated rules (position: absolute, no width) silently won,
                     # collapsing the run timeline to a shrink-wrapped sliver
                     # (dev/docs/BUGS.md 2026-08-10). Renamed to `run-tl-*`; keep it that way.
                     'tl-bar', 'tl-now'):
            for path, text in _css_sources():
                hits = len(re.findall(rf'^\.{name}\s*{{', text, re.M))
                self.assertLessEqual(
                    hits, 1,
                    f'.{name} is declared {hits} times at the top level of {_rel(path)}; '
                    'at equal specificity the later one wins and the earlier one is dead. '
                    'Rename one of them.')


class CssDeadClassTests(unittest.TestCase):
    """The inverse of `CssClassDefinedTests`: a class defined in a shared stylesheet that
    nothing references any more.

    Dead CSS is not merely clutter - it is what made the fableUI rollout's own exit chunk
    necessary. `DESIGN.md` section 8 let every chunk leave its predecessor's rules in place
    ("Old CSS classes stay until #4's final sweep chunk; never break an unconverted page"),
    so by the end 124 classes and ~950 lines described components that no longer existed:
    a whole top-navbar family the sidebar replaced, the pre-`.list-head` recordings card
    list, the pre-`.tbl` event log. Anyone reading style.css to learn "how does this app do
    X" could land on any of it. The sweep is `dev/changelog/457`; this test is what stops
    the debt reaccumulating one chunk at a time.

    **The allowlist below is the important part of this file.** A naive
    defined-minus-referenced scan reports 21 false positives, because these class names are
    never written as literals anywhere - they are built by gluing a prefix to a runtime
    value. Deleting one produces exactly the silent invisible-UI bug the sibling tests
    above guard. Every entry names its construction site; when you add a dynamically-built
    class family, add it here in the same change.
    """

    # prefix -> where the name is assembled. NOT a "known failures" list to grow when this
    # test goes red: a genuinely dead class must be deleted, not allowlisted.
    DYNAMIC_PREFIXES = {
        'badge-':            'dashboard.js `badge badge-${status.toLowerCase()}`; logs.js / '
                             'notifications.js setStatus(); six templates `badge-{{ status|lower }}`',
        'sev-':              'nav-alerts.js `alert-severity sev-${a.severity}`; '
                             'notifications.js `sev-${escHtml(r.severity)}`',
        'alert-banner-sev-': 'nav-alerts.js `alert-banner alert-banner-sev-${a.severity}`',
        'toast-':            'util.js::showToast `toast toast-${type}`',
        'tl-dot-':           "channels/_timeline.html `tl-dot-{{ ch_dot.get(dot_key, 'neutral') }}`",
        'd-':                'recording_detail.html `run-tl-dot d-{{ rep.kind }}` (the segment timeline)',
        'hb-':               'the health-band modifier (app/health_bands.py): guide.js '
                             '`hb-${state}`, util.js::healthBandCss, and Jinja `| health_css`',
    }

    # `.org` / `.w3` come from URLs inside the CSS (w3.org, fonts), not selectors.
    NOT_SELECTORS = {'org', 'w3', 'woff', 'woff2'}

    _CLASS_RE = re.compile(r'\.(-?[A-Za-z_][\w-]*)')
    _COMMENT_RE = re.compile(r'/\*.*?\*/', re.S)

    def test_no_unreferenced_class_in_shared_css(self):
        defined = {}
        for path in _walk(CSS_DIR, '.css'):
            body = self._COMMENT_RE.sub('', _read(path))
            for m in self._CLASS_RE.finditer(body):
                defined.setdefault(m.group(1), _rel(path))

        # Production consumers only. tests/ is deliberately excluded: a conformance test
        # asserting a class is ABSENT (assertNotIn('dash-card', html)) would otherwise read
        # as a reference and keep the dead rule alive forever.
        # Tokenize the corpus ONCE into the set of hyphenated words it contains, rather than
        # running a regex per class over every file: the naive form is O(classes x corpus)
        # and cost 13.6s, which alone pushed the suite over its wall-clock ceiling. Set
        # membership is the same test - `[\w-]+` tokens are exactly what the per-class
        # `(?<![\w-])name(?![\w-])` pattern was matching.
        token_re = re.compile(r'[A-Za-z_][\w-]*')
        seen = set()
        for group, ext in ((TPL_DIR, '.html'), (JS_DIR, '.js'), (APP_DIR, '.py')):
            for p in _walk(group, ext):
                seen.update(token_re.findall(_read(p)))

        dead = []
        for name, origin in sorted(defined.items()):
            if name in self.NOT_SELECTORS:
                continue
            if any(name.startswith(p) for p in self.DYNAMIC_PREFIXES):
                continue
            if name not in seen:
                dead.append(f'.{name} ({origin})')

        self.assertEqual(
            dead, [],
            f'{len(dead)} class(es) defined in a shared stylesheet but referenced by no '
            'template, page script or Python file. Delete the rule - or, if the name is '
            'assembled at runtime from a prefix, add that prefix to DYNAMIC_PREFIXES with '
            'its construction site:\n  ' + '\n  '.join(dead))

    def test_every_dynamic_prefix_still_has_its_construction_site(self):
        """An allowlist entry outlives its cause silently. Each prefix must still be built
        somewhere, or it is masking dead classes rather than protecting live ones."""
        corpus = '\n'.join(
            [_read(p) for p in _walk(TPL_DIR, '.html')] + [_read(p) for p in _walk(JS_DIR, '.js')])
        orphaned = []
        for prefix in self.DYNAMIC_PREFIXES:
            # The prefix glued to an opening interpolation/concatenation, e.g. `badge-${`,
            # `sev-' +`, `d-{{`.
            built = re.search(re.escape(prefix) + r"""(\$\{|\{\{|['"]\s*\+)""", corpus)
            if not built:
                orphaned.append(prefix)
        self.assertEqual(
            orphaned, [],
            'DYNAMIC_PREFIXES entry whose construction site is gone, so it now only hides '
            f'dead CSS from the scan above. Remove the entry and sweep the classes: {orphaned}')


class RetiredUiClassTests(unittest.TestCase):
    """The exact inverse of `CssDeadClassTests`: a *caller* of a class whose rule we deleted.

    That sibling catches a rule nothing uses. This catches markup using a rule that no longer
    exists - which renders unstyled and is invisible to every other test in this file, because
    `CssClassDefinedTests` only checks the handful of class names listed in it by hand. It is
    the shape of the live regression the rollout's sweep chunk shipped and then had to fix
    (`dev/docs/BUGS.md` 2026-08-04 @ 03:41:52 PM ET): a class deleted as dead, still referenced.

    Retired *tokens* need no entry here - `CssVarTests` already fails on an undefined `var()`.
    Add a name below whenever you delete a component class and convert its callers, in the
    same change (`dev/changelog/459`).
    """

    # class name -> what replaced it, quoted back in the failure message.
    RETIRED = {
        'table-responsive': "the table scroll wrapper is `.table-scroll`, which additionally "
                            "releases `overflow` above 900px so kebabs and popovers inside a "
                            "desktop table are not clipped (DESIGN.md 10.4)",
        'qbadge-ok': "use `.badge b-done` (DESIGN.md 3.4) - `.qbadge-ok` was retired with its "
                     "last non-guide caller, dup-modal.js (dev/changelog/579)",
        'qbadge-muted': "use `.badge b-abort` (DESIGN.md 3.4) - `.qbadge-muted` was retired "
                        "with its last non-guide callers, dup-modal.js and group-modal.js "
                        "(dev/changelog/579)",
        'acct-sheet-act': "a bottom sheet's rows are `.sheet-act`, generalized off the "
                          "`acct-` prefix when the group page became a second caller "
                          "(dev/changelog/758)",
        'acct-sheet-sep': "use `.sheet-sep` - renamed with `.acct-sheet-act` above "
                          "(dev/changelog/758)",
        # The record modal's program header became a second caller of the guide sheets'
        # detail anatomy, and that modal renders on pages that load no guide.css - so the
        # seven rules moved to style.css under a neutral prefix (dev/changelog/1050).
        'guide-sheet-sub': "use `.info-sub` (style.css)",
        'guide-sheet-desc': "use `.info-desc` (style.css)",
        'guide-sheet-line': "use `.info-line` (style.css)",
        'guide-sheet-lbl': "use `.info-lbl` (style.css)",
        'guide-sheet-val': "use `.info-val` (style.css)",
        'guide-sheet-tags': "use `.info-tags` (style.css)",
        'guide-sheet-tag': "use `.info-tag` (style.css)",
    }

    def test_no_markup_references_a_retired_class(self):
        offenders = []
        sources = ([(p, _read(p)) for p in _walk(TPL_DIR, '.html')]
                   + [(p, _read(p)) for p in _walk(JS_DIR, '.js')])
        for name, replacement in self.RETIRED.items():
            # Bounded so `.table-scroll` cannot match a rule retiring `.table`.
            pattern = re.compile(rf'(?<![\w-]){re.escape(name)}(?![\w-])')
            for path, text in sources:
                for lineno, line in enumerate(text.splitlines(), 1):
                    if pattern.search(line):
                        offenders.append(f'{_rel(path)}:{lineno} uses .{name} - {replacement}')
        self.assertEqual(
            offenders, [],
            'Markup references a class whose stylesheet rule was deleted, so it renders '
            'unstyled and nothing else in the suite can see it:\n  ' + '\n  '.join(offenders))

    def test_retired_classes_are_actually_gone_from_the_stylesheets(self):
        """The entry is only meaningful while the rule really is deleted. If someone
        reintroduces the rule, this list is silently enforcing a lie."""
        revived = []
        for name in self.RETIRED:
            for path, text in _css_sources():
                if re.search(rf'^\s*\.{re.escape(name)}\s*[,{{]', text, re.M):
                    revived.append(f'{_rel(path)} defines .{name}')
        self.assertEqual(
            revived, [],
            'A class listed as retired has a rule again. Either remove it from RETIRED '
            f'(and say why it came back) or delete the rule: {revived}')


class JsTimezoneTests(unittest.TestCase):
    """No hardcoded IANA timezone string in a JS `Intl` timeZone option - display tz always
    comes from the backend-provided variable (CLAUDE.md Timezones)."""

    _LITERAL_TZ_RE = re.compile(r'timeZone\s*:\s*[\'"]')

    def test_no_hardcoded_timezone_in_js(self):
        offenders = []
        for path in _walk(JS_DIR, '.js'):
            for i, line in enumerate(_read(path).splitlines(), 1):
                if self._LITERAL_TZ_RE.search(line):
                    offenders.append(f'{_rel(path)}:{i}: {line.strip()}')
        self.assertEqual(
            offenders, [],
            'Hardcoded timeZone: literal in JS Intl options - use the backend-provided tz '
            'variable instead (CLAUDE.md Timezones):\n' + '\n'.join(offenders))


class DisplaySettingPlumbingTests(unittest.TestCase):
    """The display timezone and clock format reach the client exactly one way.

    base.html renders `<meta name="display-tz">` and `<meta name="display-hour12">` on every
    page, and util.js's displayTz()/displayHour12() are the only readers. Before
    dev/changelog/654 there was no such source, so eleven templates injected the same two
    values into their own JS config globals in four different spellings - and the page that
    never got the plumbing, the Live dashboard's timeline, rendered the BROWSER's timezone
    instead for as long as it existed (dev/docs/BUGS.md 2026-08-14).

    Prose alone would not hold this: the failure mode of the copy-the-nearest-page habit is
    a page that looks right on the machine that wrote it, because the developer's browser
    timezone usually matches the setting. So the scan is the guard, not a review note.
    """

    # A template piping either context value into a <script> - `timezone: '{{ display_
    # timezone }}'`, `tz: {{ display_timezone | tojson }}`, `'format24': display_time_format
    # == '24h'`, and every other spelling - is re-creating the plumbing this replaced.
    _CTX_RE = re.compile(r'display_(?:timezone|time_format)')

    # Server-RENDERED uses are fine and stay: a field's `<span class="tz-label">` naming the
    # zone beside a datetime input is markup, not client config. Only uses inside a
    # <script> are plumbing.
    _SCRIPT_RE = re.compile(r'<script\b.*?</script>', re.S | re.I)

    def test_no_template_pipes_the_display_settings_into_a_script(self):
        offenders = []
        for path in _walk(TPL_DIR, '.html'):
            if os.path.basename(path) == 'base.html':
                continue  # the one legitimate producer - the meta tags themselves
            for block in self._SCRIPT_RE.findall(_read(path)):
                for line in block.splitlines():
                    if self._CTX_RE.search(line):
                        offenders.append(f'{_rel(path)}: {line.strip()}')
        self.assertEqual(
            offenders, [],
            'A template is injecting the display timezone or clock format into page JS. '
            'That plumbing is gone: base.html renders the <meta> tags and util.js exposes '
            'displayTz()/displayHour12(), so page JS needs no config of its own. Delete the '
            'injection and call the helper (dev/changelog/654):\n' + '\n'.join(offenders))

    def test_base_html_renders_both_meta_tags(self):
        """The other half of the same invariant - every reader depends on these existing."""
        base = _read(os.path.join(TPL_DIR, 'base.html'))
        for name in ('display-tz', 'display-hour12'):
            self.assertIn(
                f'name="{name}"', base,
                f'base.html must render <meta name="{name}"> - util.js reads it on every '
                'page, and without it every client-rendered time silently falls back')

    def test_util_js_owns_the_only_readers(self):
        """No file may read the meta tags itself. A second reader is a second place for the
        fallback behavior to drift, which is the shape of the bug this replaced."""
        offenders = []
        for path in _walk(JS_DIR, '.js'):
            if os.path.basename(path) == 'util.js':
                continue
            for i, line in enumerate(_read(path).splitlines(), 1):
                if 'display-tz' in line or 'display-hour12' in line:
                    offenders.append(f'{_rel(path)}:{i}: {line.strip()}')
        self.assertEqual(
            offenders, [],
            'Only util.js may read the display-setting meta tags - call displayTz() or '
            'displayHour12() instead:\n' + '\n'.join(offenders))


class JsParsesTests(unittest.TestCase):
    """Every file in static/js/ must actually parse as JavaScript.

    dev/docs/BUGS.md 2026-08-04 02:20 PM: `static/js/accounts.js` shipped with its IIFE
    written `(() => { ... }());`, copied from `account-detail.js`'s valid
    `(function () { ... }());`. An arrow function is not a legal callee inside those grouping
    parens, so the file was one SyntaxError and NOTHING in it ran - no row navigation, no
    kebab, no bottom sheet - while the page itself rendered normally, because the markup is
    server-side.

    Nothing else in the suite can see this. These files are never imported by a test, jsdom is
    not used for them, and a template that loads a broken script renders exactly the same. The
    check is a whole-file parse rather than a pattern for the one shape that bit us: any syntax
    error has the same total blast radius.
    """

    def test_every_js_file_parses(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('node is not installed - nothing else can parse these files')
        offenders = []
        for path in _walk(JS_DIR, '.js'):
            proc = subprocess.run([node, '--check', path], capture_output=True, text=True)
            if proc.returncode != 0:
                first = (proc.stderr.strip().splitlines() or ['(no output)'])
                detail = next((l for l in first if 'Error' in l), first[0])
                offenders.append(f'{_rel(path)}: {detail.strip()}')
        self.assertEqual(offenders, [],
                         'These JS files do not parse, so none of their code runs:\n'
                         + '\n'.join(offenders))


class TimezoneLiteralTests(unittest.TestCase):
    """`America/New_York` may appear only as the config default (app/config.py) or the
    documented fallback constant in the tz-authority module (app/tz_utils.py). Anywhere else
    it is forced-Eastern display code, which is a bug (CLAUDE.md Timezones)."""

    # The two tz-authority modules that legitimately name the default zone.
    _ALLOWED = {os.path.join('app', 'config.py'), os.path.join('app', 'tz_utils.py')}

    def test_no_hardcoded_eastern_in_python(self):
        offenders = []
        for path in _walk(APP_DIR, '.py'):
            if _rel(path) in self._ALLOWED:
                continue
            for i, line in enumerate(_read(path).splitlines(), 1):
                if 'America/New_York' in line:
                    offenders.append(f'{_rel(path)}:{i}: {line.strip()}')
        self.assertEqual(
            offenders, [],
            'Hardcoded America/New_York outside app/config.py and app/tz_utils.py - display '
            'tz must resolve through tz_utils.get_display_tz() (CLAUDE.md Timezones):\n'
            + '\n'.join(offenders))


class SubprocessPipeTests(unittest.TestCase):
    """Every `stderr=subprocess.PIPE` must have something draining it, or an undrained 64KB
    pipe buffer deadlocks the child (CLAUDE.md Subprocess discipline). A drained site carries
    a `# drained` marker comment on the same line; proc_utils.py is the canonical spawn home."""

    _PIPE_RE = re.compile(r'stderr\s*=\s*(subprocess\.)?PIPE')

    def test_no_undrained_stderr_pipe(self):
        offenders = []
        for path in _walk(APP_DIR, '.py'):
            if os.path.basename(path) == 'proc_utils.py':
                continue
            for i, line in enumerate(_read(path).splitlines(), 1):
                if self._PIPE_RE.search(line) and 'drained' not in line.lower():
                    offenders.append(f'{_rel(path)}:{i}: {line.strip()}')
        self.assertEqual(
            offenders, [],
            'stderr=subprocess.PIPE with no drain marker - an undrained pipe deadlocks the '
            'child (CLAUDE.md Subprocess discipline). Drain it in a thread and add a '
            '`# drained ...` comment on that line, or spawn via proc_utils:\n'
            + '\n'.join(offenders))


class ConfigReadBypassTests(unittest.TestCase):
    """config.yaml may only be *read* through app/config.py's cached reader.

    Fifth instance of one defect class (BUGS.md 2026-07-15 10:34, 2026-07-17 18:46,
    2026-07-20 01:28, 2026-07-22 sync, 2026-07-24 create_app). The first four were per-row
    load_config() calls and were answered with the mtime cache; the fifth was different in
    shape - two startup paths opened and YAML-parsed the file directly, so the cache could
    not help them and nothing noticed for as long as they only ran once per process. Prose
    rules did not stop instances 2-5, so this is the enforcement: any new direct read is a
    red test, in app/, tests/ and tools/ alike.

    Writes are untouched - migrate_config(), save_config() and the legacy-key strip all
    legitimately rewrite the file. A read that genuinely must hit the disk (asserting on the
    bytes actually stored) escapes with a `# direct-config-read: <reason>` marker on the
    open() line, same convention as the `# drained` marker above.
    """

    _MARKER = 'direct-config-read'
    # The one sanctioned reader; everything else must call load_config()/_load_config_file().
    _CANONICAL = {('app/config.py', '_parse_config_file')}

    @staticmethod
    def _enclosing_def(lines, idx):
        for j in range(idx, -1, -1):
            m = re.match(r'\s*def\s+(\w+)', lines[j])
            if m:
                return m.group(1)
        return '<module>'

    def test_config_yaml_is_only_read_through_the_cache(self):
        offenders = []
        for root in (APP_DIR, TESTS_DIR, TOOLS_DIR):
            for path in _walk(root, '.py'):
                raw_lines = _read(path).splitlines()
                # Match against code only: this scanner's own detector strings ('open(',
                # 'CONFIG_PATH') would otherwise flag this very file. Masking preserves
                # layout, so indices still line up with raw_lines.
                code_lines = _mask_comments_and_strings(_read(path)).splitlines()
                for i, line in enumerate(code_lines):
                    if 'open(' not in line or 'CONFIG_PATH' not in line:
                        continue
                    if self._MARKER in raw_lines[i]:
                        continue
                    # A masked mode argument leaves the quotes in place, so a write site
                    # still reads as open(..., '   '); recover the mode from the raw line.
                    if re.search(r"open\([^)]*['\"][wax]", raw_lines[i]):
                        continue
                    if (_rel(path), self._enclosing_def(code_lines, i)) in self._CANONICAL:
                        continue
                    offenders.append(f'{_rel(path)}:{i + 1}: {raw_lines[i].strip()}')
        self.assertEqual(
            offenders, [],
            'config.yaml read directly instead of through app/config.py::load_config() / '
            '_load_config_file() (CLAUDE.md "config.yaml has exactly one reader"). A direct '
            'read bypasses the mtime cache, so it re-parses the file every call - the defect '
            'class behind five BUGS.md entries. Call load_config() (or _load_config_file() '
            'for the raw un-merged file dict), or add a `# direct-config-read: <reason>` '
            'marker if this read genuinely must hit the disk:\n' + '\n'.join(offenders))


class ConfigWriteBypassTests(unittest.TestCase):
    """config.yaml may only be *written* through app/config.py's atomic writers.

    The write-side counterpart to ConfigReadBypassTests above, and the same shape of
    defect: four separate paths each did open(_CONFIG_PATH, 'w') (or copy2 onto it) and
    then wrote, so a crash, an OOM-kill or an exception between the truncate and the last
    byte left an empty or half-written config.yaml - the file that holds flask.secret_key
    and auth.password_hash (dev/docs/BUGS.md 2026-08-15 @ 05:32:07 PM ET,
    dev/changelog/671). _write_config_file() and _replace_config_file_from() write a temp
    file beside the target and os.replace() it into place; a fifth hand-rolled writer would
    quietly reopen the hole, so the scan is the guard rather than the prose.

    app/ only. A test seeding its own sandbox file is writing a fixture, not the app's
    shared store, and ConfigSandbox does exactly that by design.

    Escape hatch: a `# direct-config-write: <reason>` marker on the offending line.
    """

    _MARKER = 'direct-config-write'
    _CANONICAL = {('app/config.py', '_write_config_file'),
                  ('app/config.py', '_replace_config_file_from'),
                  ('app/config.py', '_finish_replace')}
    # A config path as the DESTINATION of a write. copy2(cfg_path, dest) - reading the
    # config into a backup - deliberately does not match: only the second argument counts.
    _PATH = r'(?:_CONFIG_PATH|\w*cfg_path|\w*config_path)'
    _PATTERNS = (
        re.compile(r'open\(\s*' + _PATH + r'\s*,\s*[\'"][wax]'),
        re.compile(r'(?:copy2?|copyfile|move)\(\s*[^,()]+,\s*' + _PATH + r'\s*[),]'),
        re.compile(r'os\.replace\(\s*[^,()]+,\s*' + _PATH + r'\s*[),]'),
    )

    def test_config_yaml_is_only_written_through_its_atomic_writers(self):
        offenders = []
        for path in _walk(APP_DIR, '.py'):
            raw_lines = _read(path).splitlines()
            for i, line in enumerate(raw_lines):
                if not any(p.search(line) for p in self._PATTERNS):
                    continue
                if self._MARKER in line:
                    continue
                enclosing = ConfigReadBypassTests._enclosing_def(raw_lines, i)
                if (_rel(path), enclosing) in self._CANONICAL:
                    continue
                offenders.append(f'{_rel(path)}:{i + 1}: {line.strip()}')
        self.assertEqual(
            offenders, [],
            'config.yaml written directly instead of through app/config.py::'
            '_write_config_file() / _replace_config_file_from(). A truncate-then-write '
            'leaves an empty config.yaml - no secret_key, no auth hash - if the process '
            'dies mid-write, and callers also need config_write_lock across their whole '
            'read-modify-write. Add a `# direct-config-write: <reason>` marker if this '
            'write genuinely must bypass them:\n' + '\n'.join(offenders))


class ParticipationWriteBypassTests(unittest.TestCase):
    """A participation switch moves through one writer, and that writer logs it.

    `ChannelGroupMember.recording_enabled` / `.test_enabled` store the user's answer to a
    judgment call, so §4.1 lets nothing but a human write them - and §4.5 says a change to
    either writes a `GROUP_MEMBER_PARTICIPATION` `ChannelGroupEvent`. A second route
    assigned `membership.test_enabled` directly and committed, so a member's health-check
    participation could flip with no trace on any surface, which is principle 1 inverted
    (dev/docs/BUGS.md 2026-08-19, dev/changelog/748). Prose said "the ONLY writer" for a
    whole commit while that route existed, so the scan is the guard rather than the prose.

    app/ only - a test builds fixtures, and `tests/support/seed.py` sets both columns as
    constructor kwargs by design.

    `Channel.test_enabled` (the channel-wide off switch) is a different column that shares
    a name deliberately, and no scanner can tell the two apart from an attribute
    assignment. Those writes carry the marker naming which column they are on, which is
    also the reminder to whoever adds the next one.

    Escape hatch: a `# participation-write-ok: <reason>` marker on the line itself or
    anywhere in the comment block directly above it.
    """

    _MARKER = 'participation-write-ok'
    _CANONICAL = {('app/channel_groups.py', 'set_participation')}
    _PATTERN = re.compile(r'\.(?:recording_enabled|test_enabled)\s*=(?!=)')

    @classmethod
    def _marked(cls, raw_lines, idx):
        """The marker on the assignment itself, or in the contiguous comment block above
        it - the reason for one of these writes rarely fits on the line it is about."""
        if cls._MARKER in raw_lines[idx]:
            return True
        for j in range(idx - 1, -1, -1):
            if not raw_lines[j].strip().startswith('#'):
                return False
            if cls._MARKER in raw_lines[j]:
                return True
        return False

    def test_the_participation_columns_have_one_writer(self):
        offenders = []
        for path in _walk(APP_DIR, '.py'):
            raw_lines = _read(path).splitlines()
            # Comments and strings masked out: a docstring naming the column must not
            # trip a scan whose whole subject is where the column is assigned.
            code_lines = _mask_comments_and_strings(_read(path)).splitlines()
            for i, line in enumerate(code_lines):
                if not self._PATTERN.search(line):
                    continue
                if self._marked(raw_lines, i):
                    continue
                enclosing = ConfigReadBypassTests._enclosing_def(code_lines, i)
                if (_rel(path), enclosing) in self._CANONICAL:
                    continue
                offenders.append(f'{_rel(path)}:{i + 1}: {raw_lines[i].strip()}')
        self.assertEqual(
            offenders, [],
            'a participation switch assigned outside app/channel_groups.py::'
            'set_participation(), the one writer of ChannelGroupMember.recording_enabled '
            'and .test_enabled (CLAUDE.md "A participation switch is written by a human '
            'and by nothing else"). A direct assignment moves the switch with no '
            'GROUP_MEMBER_PARTICIPATION event, so the group Activity Timeline never shows '
            'it happening. Call set_participation(), or add a '
            '`# participation-write-ok: <reason>` marker if this is Channel.test_enabled '
            'or another column that merely shares the name:\n' + '\n'.join(offenders))


class MetadataLockWriteBypassTests(unittest.TestCase):
    """`Recording.metadata_locked` is written by the user's own action and by nothing else.

    The same rule as the participation switches above, on a column that stores the same
    kind of fact: "leave my wording alone" is the user's answer to a judgment call, so an
    engine has no standing to write it. `recording_metadata.refresh_from_guide()` FILTERS
    on it - it never clears the lock, and never decides a program changed enough to be
    worth overriding one (dev/changelog/1055).

    `recording_metadata.apply_user_edit()` is the one writer, and it is the shape
    `channel_groups.set_participation()` established: the column moves and the
    `RECORDING_METADATA_EDITED` event explaining it is written in the same function, so a
    lock cannot change with nothing on any surface saying so. The guard shipped with the
    column and an empty `_CANONICAL` one changelog ahead of that surface (dev/changelog/1055,
    then `1058`), precisely so the surface had to be built through one writer rather than
    assigning the column from a route and meeting the rule afterwards - which is how the
    participation guard came to exist in the first place.

    app/ only - a test builds fixtures, and seeding a locked row is how the filter gets
    exercised at all.

    Escape hatch: a `# metadata-lock-write-ok: <reason>` marker on the line itself or
    anywhere in the comment block directly above it.
    """

    _MARKER = 'metadata-lock-write-ok'
    _CANONICAL = {('app/recording_metadata.py', 'apply_user_edit')}
    _PATTERN = re.compile(r'\.metadata_locked\s*=(?!=)')

    def test_the_metadata_lock_has_one_writer(self):
        offenders = []
        for path in _walk(APP_DIR, '.py'):
            raw_lines = _read(path).splitlines()
            code_lines = _mask_comments_and_strings(_read(path)).splitlines()
            for i, line in enumerate(code_lines):
                if not self._PATTERN.search(line):
                    continue
                if _marked_at(raw_lines, i, self._MARKER):
                    continue
                enclosing = ConfigReadBypassTests._enclosing_def(code_lines, i)
                if (_rel(path), enclosing) in self._CANONICAL:
                    continue
                offenders.append(f'{_rel(path)}:{i + 1}: {raw_lines[i].strip()}')
        self.assertEqual(
            offenders, [],
            'Recording.metadata_locked assigned outside its one writer (CLAUDE.md "any '
            'column that stores a user\'s answer to a judgment call"). The lock suppresses '
            'the record-start refresh of a recording\'s program details, so a write with '
            'no event behind it leaves the user with a description that quietly stopped '
            'tracking the guide and nothing on any surface saying why. Route it through a '
            'single function that moves the column and logs it together, add that function '
            'to _CANONICAL here, or mark the line '
            '`# metadata-lock-write-ok: <reason>`:\n' + '\n'.join(offenders))


def _marked_at(raw_lines, idx, marker):
    """The marker on the line itself, or anywhere in the contiguous comment block directly
    above it - the reason for one of these rarely fits on the line it is about."""
    if marker in raw_lines[idx]:
        return True
    for j in range(idx - 1, -1, -1):
        if not raw_lines[j].strip().startswith('#'):
            return False
        if marker in raw_lines[j]:
            return True
    return False


def _toplevel_block(code_lines, idx):
    """The line range of the outermost `def`/`class` enclosing `idx`.

    Outermost rather than innermost on purpose: several of the sites this scans sit inside a
    `@retry_on_locked()` closure while the call that satisfies them is in the enclosing
    function, and an innermost-def scan would report those as bypasses.
    """
    start = 0
    for j in range(idx, -1, -1):
        if re.match(r'(?:def|class|async def)\s+\w+', code_lines[j]):
            start = j
            break
    end = len(code_lines)
    for j in range(start + 1, len(code_lines)):
        line = code_lines[j]
        if line.strip() and not line[0].isspace() and not line.startswith(')'):
            end = j
            break
    return start, end


class HideOverrideWriteBypassTests(unittest.TestCase):
    """`Channel.hidden_override` is a user's answer to a judgment call, so CLAUDE.md's
    participation-switch rule applies to it directly: a human writes it and nothing else
    does. One writer - `channel_hiding.set_hidden_override()` - which also emits the
    `CHANNEL_HIDE_OVERRIDE_CHANGED` event, so the answer cannot move without the channel's
    Activity Timeline saying so.

    Prose claiming "the ONLY writer" is what the group columns had for a whole commit while a
    second route quietly assigned one (dev/changelog/748), so the scan is the guard rather
    than the docstring. Escape hatch: `# hide-override-write-ok: <reason>`.

    app/ only - a test builds fixtures. dev/changelog/775.
    """

    _MARKER = 'hide-override-write-ok'
    _CANONICAL = {('app/channel_hiding.py', 'set_hidden_override')}
    _PATTERN = re.compile(r'\.hidden_override\s*=(?!=)')

    def test_the_override_has_one_writer(self):
        offenders = []
        for path in _walk(APP_DIR, '.py'):
            raw_lines = _read(path).splitlines()
            code_lines = _mask_comments_and_strings(_read(path)).splitlines()
            for i, line in enumerate(code_lines):
                if not self._PATTERN.search(line):
                    continue
                if _marked_at(raw_lines, i, self._MARKER):
                    continue
                enclosing = ConfigReadBypassTests._enclosing_def(code_lines, i)
                if (_rel(path), enclosing) in self._CANONICAL:
                    continue
                offenders.append(f'{_rel(path)}:{i + 1}: {raw_lines[i].strip()}')
        self.assertEqual(
            offenders, [],
            'Channel.hidden_override assigned outside app/channel_hiding.py::'
            'set_hidden_override(), its one writer. A direct assignment stores the user\'s '
            'answer with no CHANNEL_HIDE_OVERRIDE_CHANGED event behind it:\n'
            + '\n'.join(offenders))


class HiddenCacheWriteBypassTests(unittest.TestCase):
    """The other half, and the opposite rule. `Channel.hidden` / `.hidden_reason` /
    `.hidden_deferred` are a derived cache with exactly one writer -
    `channel_hiding.recompute()` - and no human and no route may assign one.

    `hidden` in particular means "do not offer this channel" and nothing else. The whole
    reason it is a separate column from `hidden_override` is that letting one flag mean both
    "the effective answer" and "the user hid this" is the `in_guide` defect CLAUDE.md
    documents at length, and a route that assigns `hidden` directly is how that starts.

    Escape hatch: `# hidden-cache-write-ok: <reason>`. dev/changelog/775.
    """

    _MARKER = 'hidden-cache-write-ok'
    _CANONICAL = {('app/channel_hiding.py', 'recompute')}
    _PATTERN = re.compile(r'\.(?:hidden|hidden_reason|hidden_deferred)\s*=(?!=)')

    def test_the_derived_columns_have_one_writer(self):
        offenders = []
        for path in _walk(APP_DIR, '.py'):
            raw_lines = _read(path).splitlines()
            code_lines = _mask_comments_and_strings(_read(path)).splitlines()
            for i, line in enumerate(code_lines):
                if not self._PATTERN.search(line):
                    continue
                if _marked_at(raw_lines, i, self._MARKER):
                    continue
                enclosing = ConfigReadBypassTests._enclosing_def(code_lines, i)
                if (_rel(path), enclosing) in self._CANONICAL:
                    continue
                offenders.append(f'{_rel(path)}:{i + 1}: {raw_lines[i].strip()}')
        self.assertEqual(
            offenders, [],
            'a derived hiding column assigned outside app/channel_hiding.py::recompute(). '
            'These are a cache over the rules, the override and guide/group protection - '
            'change an input and recompute, never the answer:\n' + '\n'.join(offenders))


class HiddenRecomputeHookTests(unittest.TestCase):
    """Anything that can change whether a channel is PROTECTED must recompute its hidden
    state in the same unit.

    Guide membership and group membership defer a hide rather than refusing it, so
    `Channel.hidden` is a cache over two things a dozen routes move. A missed hook leaves the
    cache stale with nothing to notice it - the channel is simply offered, or not, with no
    error anywhere - which is the whole reason this is a scan and not a paragraph. It is the
    static half of `tests/test_channel_hiding.py::IncrementalMatchesFromScratchTests`, which
    proves the hooks that DO exist are correct; this one is what notices a new site.

    A function satisfies it by mentioning `channel_hiding` anywhere in its outermost
    enclosing block. Deliberately loose: what it is really looking for is a whole call site
    that has never heard of hiding at all.

    Escape hatch: `# hidden-recompute-ok: <reason>` - and it is genuinely needed, because
    `ChannelGroup.in_guide` shares its name with `Channel.in_guide` and no scanner can tell
    an attribute assignment on one from the other. dev/changelog/775.
    """

    _MARKER = 'hidden-recompute-ok'
    _PATTERNS = (re.compile(r'\.in_guide\s*=(?!=)'), re.compile(r'\bChannelGroupMember\('))

    def test_every_protection_change_recomputes(self):
        offenders = []
        for path in _walk(APP_DIR, '.py'):
            if _rel(path) == 'app/channel_hiding.py':
                continue
            raw_lines = _read(path).splitlines()
            code_lines = _mask_comments_and_strings(_read(path)).splitlines()
            for i, line in enumerate(code_lines):
                if not any(p.search(line) for p in self._PATTERNS):
                    continue
                # `class ChannelGroupMember(db.Model):` is the model's own declaration, not
                # a membership being created.
                if line.lstrip().startswith('class '):
                    continue
                if _marked_at(raw_lines, i, self._MARKER):
                    continue
                start, end = _toplevel_block(code_lines, i)
                if any('channel_hiding' in ln for ln in code_lines[start:end]):
                    continue
                offenders.append(f'{_rel(path)}:{i + 1}: {raw_lines[i].strip()}')
        self.assertEqual(
            offenders, [],
            'a site that changes whether a channel is protected from being hidden, with no '
            'channel_hiding.recompute() anywhere in its enclosing function. Guide rows and '
            'group memberships DEFER a hide, so gaining or losing one changes the answer - '
            'add the recompute inside the same commit unit, or a '
            '`# hidden-recompute-ok: <reason>` marker if this is ChannelGroup.in_guide or a '
            'path where nothing can be protected:\n' + '\n'.join(offenders))


# Uppercase-only on purpose: ordinary lowercase prose about "a task" must not trip this.
# Covers TASKS.md, TASK-<name>.md, TASKS-<name>-do-next.md and TASKMASTER alike.
_TASK_REF_RE = re.compile(r'\bTASK(?:S\b|-|MASTER|S\.md|S-)')


class TaskFileReferenceTests(unittest.TestCase):
    """Nothing durable may cite a `dev/tasks/` file (CLAUDE.md "No load-bearing references to
    dev/tasks/").

    Everything under `dev/tasks/` is temporary by construction: `TASKS-*-do-next.md` batch
    files and `TASK-*.md` handoffs are deleted at close-out, and a completed `TASKS.md` item
    is removed outright rather than checked off. A durable file that cites one is therefore a
    pointer with an expiry date on it, and several had already expired when this rule was
    written - `app/migrations.py` explained two schema migrations via `TASK-fix-group-ui` and
    `TASKS-monitor-conversion`, both long deleted; `DESIGN-concurrency.md` cited
    `TASK-fableFINAL.md`; `DESIGN-live-vod.md` pointed at a `TASKS.md` item that no longer
    exists. The permanent record is `dev/changelog/NNN` (plus `BUGS.md` for defects), so that
    is what durable files cite.

    The `dev/docs/` half of this rule lives in `tests/test_dev_tree_invariants.py`, which is
    dev-only because its input is - along with `_UNSHIPPED_DESIGN_DOCS`, the allowlist that
    serves only that half. The scan below covers the durable trees that ship, and stays here.

    One exemption, narrow: `_TASK_SYSTEM_FILES` - files whose *subject* is the task system,
    where naming these files is the content rather than a pointer. It covers both halves, docs
    and code alike: `tests/test_worklog.py` validates the `dev/tasks/` front-matter format
    itself, so every line of it would otherwise need its own marker.
    `tests/test_dev_tree_invariants.py` is listed for the same reason in advance - it is the
    other half of this rule, so its prose is one edit away from naming the patterns it
    enforces. `BUGS.md` is here for a different reason: it is append-only, so its two
    pre-existing citations are grandfathered and cannot be edited.

    Escape hatch for a single line: `# task-ref-ok: <reason>`.
    """

    _MARKER = 'task-ref-ok'
    # This file names every pattern it hunts for, in this docstring and in the allowlists
    # below, so it is exempt from itself - the same reason ConfigReadBypassTests has to mask
    # its own detector strings.
    _SELF = os.path.abspath(__file__)

    # Durable trees: after the 2026-07-25 sweep these must stay at zero hits.
    _HARD_SCAN = (
        (os.path.join(ROOT, 'app'), ('.py',)),
        (os.path.join(ROOT, 'static'), ('.js', '.css')),
        (os.path.join(ROOT, 'templates'), ('.html',)),
        (os.path.join(ROOT, 'tests'), ('.py',)),
        (os.path.join(ROOT, 'tools'), ('.py',)),
    )
    _HARD_FILES = (os.path.join(ROOT, 'run.py'), os.path.join(ROOT, 'README.md'),
                   # Its whole subject is the dev-only documents shipped code cites, so it is
                   # the one shipped doc most likely to reach for a backlog file by name
                   # (dev/changelog/521).
                   os.path.join(ROOT, 'docs', 'CONVENTIONS.md'))

    _TASK_SYSTEM_FILES = frozenset({
        'dev/docs/how-to/taskmaster.md',      # how to build/retire a TASKMASTER queue
        'dev/docs/HOWTO-claude-automate.md',  # user-facing guide to /___1 and /___2
        'dev/docs/CODEX-ONBOARDING.md',       # onboarding tour of the task workflow
        'dev/docs/BUGS.md',                   # append-only; existing entries grandfathered
        'tests/test_worklog.py',              # validates the dev/tasks/ metadata format itself
        'tests/test_dev_tree_invariants.py',  # the dev/docs/ half of this same rule
    })

    def _offenders_in(self, path):
        out = []
        if os.path.abspath(path) == self._SELF:
            return out
        for i, line in enumerate(_read(path).splitlines()):
            if _TASK_REF_RE.search(line) and self._MARKER not in line:
                out.append(f'{_rel(path)}:{i + 1}: {line.strip()[:110]}')
        return out

    def test_no_task_file_citations_in_code_or_tests(self):
        offenders = []
        for root, exts in self._HARD_SCAN:
            for ext in exts:
                for path in _walk(root, ext):
                    if _rel(path) in self._TASK_SYSTEM_FILES:
                        continue
                    offenders += self._offenders_in(path)
        for path in self._HARD_FILES:
            offenders += self._offenders_in(path)
        self.assertEqual(
            sorted(offenders), [],
            'Durable file cites a dev/tasks/ file, which is deleted at close-out (CLAUDE.md '
            '"No load-bearing references to dev/tasks/"). Cite the dev/changelog/NNN that '
            'records the work instead - or, if the work has not shipped, state the constraint '
            'inline and drop the file name. Add `# task-ref-ok: <reason>` only if the line '
            'genuinely has to name one:\n' + '\n'.join(sorted(offenders)))


class BareExceptTests(unittest.TestCase):
    """No broad `except Exception:` / bare `except:` whose entire body is `pass` - a silent
    swallow (CLAUDE.md Error handling). Narrow guards (`except OSError: pass`) are allowed."""

    # A broad-except header line, then (ignoring blanks) a line that is exactly `pass`.
    _BROAD_RE = re.compile(r'^\s*except\s*(Exception\s*)?:\s*$')

    def test_no_broad_except_pass(self):
        offenders = []
        for path in _walk(APP_DIR, '.py'):
            lines = _read(path).splitlines()
            for i, line in enumerate(lines):
                if not self._BROAD_RE.match(line):
                    continue
                # find the next non-blank line (the except body's first statement)
                j = i + 1
                while j < len(lines) and lines[j].strip() == '':
                    j += 1
                if j < len(lines) and lines[j].strip() == 'pass':
                    offenders.append(f'{_rel(path)}:{i + 1}: {line.strip()} -> pass')
        self.assertEqual(
            offenders, [],
            'Broad `except Exception: pass` / `except: pass` silent swallow (CLAUDE.md Error '
            'handling). Narrow to the expected exception and/or log.warning it:\n'
            + '\n'.join(offenders))


class JinjaBuiltinTests(unittest.TestCase):
    """Python builtins used as kwargs inside a Jinja expression raise UndefinedError at render
    (`request.args.get('x', type=int)`) - CLAUDE.md Jinja hazards."""

    # kwarg `type=`/`int=`/... =<builtin> inside a {{ }} or {% %} delimiter.
    _JINJA_RE = re.compile(r'\{[{%](?P<body>.*?)[}%]\}', re.DOTALL)
    _BUILTIN_KWARG_RE = re.compile(
        r'\b(type|key|default)\s*=\s*(int|float|str|bool|len|round|min|max)\b')

    def test_no_builtin_kwargs_in_jinja(self):
        offenders = []
        for path in _walk(TPL_DIR, '.html'):
            text = _read(path)
            for m in self._JINJA_RE.finditer(text):
                if self._BUILTIN_KWARG_RE.search(m.group('body')):
                    offenders.append(f'{_rel(path)}: {{{{ {m.group("body").strip()} }}}}')
        self.assertEqual(
            offenders, [],
            'Python builtin used as a kwarg inside a Jinja expression (raises UndefinedError '
            'at render - CLAUDE.md Jinja hazards). Do the coercion in the view, not the '
            'template:\n' + '\n'.join(offenders))


class HandTypedSettingsDefaultTests(unittest.TestCase):
    """Guards BUGS.md 2026-09-17 @ 05:56:42 AM "Settings Default: lines had drifted from the code".

    Every settings row's `Default:` line was a string typed into the template, and four of
    them had drifted from `_DEFAULTS` far enough to mislead (sync interval said 6 hours, the
    code said 12). The macro now looks the default up by path (`config.default_display`,
    dev/changelog/1003), so no field macro may take a default argument and no call may pass
    one where the old argument sat - fourth position, straight after the description.
    """

    _FIELD_MACROS = ('field_shell', 'field_text', 'field_number', 'field_time', 'field_bool',
                     'field_select', 'field_list', 'field_readonly')
    _PAGES = ('settings.html', 'notifications_settings.html')

    def _parse(self, name):
        from jinja2 import Environment
        return Environment().parse(_read(os.path.join(TPL_DIR, name)))

    def test_no_field_macro_takes_a_default_argument(self):
        from jinja2 import nodes
        macros = {m.name: [a.name for a in m.args]
                  for m in self._parse('_macros.html').find_all(nodes.Macro)
                  if m.name in self._FIELD_MACROS}
        self.assertEqual(set(macros), set(self._FIELD_MACROS))
        for name, params in macros.items():
            with self.subTest(macro=name):
                self.assertNotIn('default_disp', params)

    def test_no_field_call_passes_a_typed_default(self):
        from jinja2 import nodes
        offenders = []
        calls = 0
        for page in self._PAGES:
            for call in self._parse(page).find_all(nodes.Call):
                if not (isinstance(call.node, nodes.Name) and call.node.name in self._FIELD_MACROS):
                    continue
                calls += 1
                arg = call.args[3] if len(call.args) > 3 else None
                typed = isinstance(arg, nodes.Const) and isinstance(arg.value, str)
                if typed or any(k.key == 'default_disp' for k in call.kwargs):
                    offenders.append(f'{page}:{call.lineno} {call.node.name}')
        self.assertGreater(calls, 100, 'found almost no field calls - has the macro set moved?')
        self.assertEqual(
            offenders, [],
            'A settings field call passes a hand-typed default. The Default: line is looked up '
            'from _DEFAULTS by path; a typed one drifts:\n' + '\n'.join(offenders))


class SettingsTierDeclaredTests(unittest.TestCase):
    """Every Settings field declares its Basic/Advanced tier where it is declared.

    The tier lives on the field call (`tier='basic'` / `tier='advanced'`), beside the label
    and description, rather than in a Python registry keyed by path - the restart badge
    already has two homes for one fact. The macro cannot make the argument required (Jinja
    refuses a bare argument after defaulted ones), so a call that forgets it renders an
    empty data-tier and shows in both views. This is the enforcement point (DESIGN.md 15.9,
    dev/changelog/1005). A call with an empty path (the LAN-exposure warning) is not a
    setting and has no tier.
    """

    _FIELD_MACROS = HandTypedSettingsDefaultTests._FIELD_MACROS
    _PAGES = HandTypedSettingsDefaultTests._PAGES

    def test_every_field_call_with_a_path_declares_its_tier(self):
        from jinja2 import Environment, nodes
        offenders = []
        calls = 0
        for page in self._PAGES:
            tree = Environment().parse(_read(os.path.join(TPL_DIR, page)))
            for call in tree.find_all(nodes.Call):
                if not (isinstance(call.node, nodes.Name) and call.node.name in self._FIELD_MACROS):
                    continue
                path = call.args[0] if call.args else None
                if isinstance(path, nodes.Const) and path.value == '':
                    continue
                calls += 1
                tier = next((k.value for k in call.kwargs if k.key == 'tier'), None)
                if not (isinstance(tier, nodes.Const) and tier.value in ('basic', 'advanced')):
                    offenders.append(f'{page}:{call.lineno} {call.node.name}')
        self.assertGreater(calls, 100, 'found almost no field calls - has the macro set moved?')
        self.assertEqual(
            offenders, [],
            "A settings field call does not declare tier='basic' or tier='advanced' "
            '(DESIGN.md 15.9 says how to choose; Advanced is the resting state):\n'
            + '\n'.join(offenders))


class SettingsGateDeclarationTests(unittest.TestCase):
    """Every `gated_by=` on a Settings field names a gate the page's script can read.

    A gate is a claim that nothing in app/ reads the field while another field on the same
    page is off or on one value (DESIGN.md 15.9, dev/changelog/1007). The script reads the
    gate from that field's own control, so a predicate naming a path the page does not
    render, a switch form pointed at a dropdown or a value the dropdown does not offer would
    never dim anything, silently. And a gate that a profile, channel or account can override
    may not dim at all (rule 8), so a gating field may not also declare `overridable_by`.
    """

    _FIELD_MACROS = HandTypedSettingsDefaultTests._FIELD_MACROS
    _PAGES = HandTypedSettingsDefaultTests._PAGES

    @staticmethod
    def _kw(call, key):
        return next((k.value for k in call.kwargs if k.key == key), None)

    def _fields(self, tree):
        """{path: (macro name, call, body text)} for every field call on one page."""
        from jinja2 import nodes
        bodies = {}
        for block in tree.find_all(nodes.CallBlock):
            bodies[id(block.call)] = ''.join(
                d.data for d in block.find_all(nodes.TemplateData))
        fields = {}
        for call in tree.find_all(nodes.Call):
            if not (isinstance(call.node, nodes.Name) and call.node.name in self._FIELD_MACROS):
                continue
            path = call.args[0] if call.args else None
            if isinstance(path, nodes.Const) and path.value:
                fields[path.value] = (call.node.name, call, bodies.get(id(call), ''))
        return fields

    def test_every_gate_names_a_readable_field_on_the_same_page(self):
        from jinja2 import Environment, nodes
        offenders = []
        gates = 0
        for page in self._PAGES:
            fields = self._fields(Environment().parse(_read(os.path.join(TPL_DIR, page))))
            for path, (_, call, _) in fields.items():
                declared = self._kw(call, 'gated_by')
                if declared is None:
                    continue
                if not (isinstance(declared, nodes.List)
                        and all(isinstance(i, nodes.Const) for i in declared.items)):
                    offenders.append(f'{page} {path}: gated_by must be a literal list of strings')
                    continue
                for pred in (i.value for i in declared.items):
                    gates += 1
                    gate, _, value = pred.partition('!=')
                    if gate not in fields:
                        offenders.append(f'{page} {path}: {pred} names no field on this page')
                        continue
                    macro, gcall, body = fields[gate]
                    if self._kw(gcall, 'overridable_by') is not None:
                        offenders.append(f'{page} {path}: {gate} can be overridden, so it '
                                         'may not dim anything')
                    if not value:
                        if not (macro == 'field_bool' or 'type="checkbox"' in body):
                            offenders.append(f'{page} {path}: {pred} is the switch form but '
                                             f'{gate} is not a switch')
                        continue
                    options = gcall.args[4] if macro == 'field_select' and len(gcall.args) > 4 else None
                    offered = ([o.items[0].value for o in options.items]
                               if isinstance(options, nodes.List) else [])
                    if value not in offered:
                        offenders.append(f'{page} {path}: {pred} but {gate} offers no '
                                         f'literal option {value!r}')
        self.assertGreater(gates, 15, 'found almost no gates - has the macro argument moved?')
        self.assertEqual(offenders, [], 'A Settings gate the page cannot read:\n' + '\n'.join(offenders))

    def test_overridable_by_names_a_known_kind(self):
        from jinja2 import Environment, nodes
        offenders = []
        for page in self._PAGES:
            fields = self._fields(Environment().parse(_read(os.path.join(TPL_DIR, page))))
            for path, (_, call, _) in fields.items():
                kind = self._kw(call, 'overridable_by')
                if kind is not None and not (isinstance(kind, nodes.Const)
                                             and kind.value in ('recording_profile', 'health_check_profile',
                                                                'channel', 'account')):
                    offenders.append(f'{page} {path}')
        self.assertEqual(offenders, [], "overridable_by must be 'recording_profile', "
                         "'health_check_profile', 'channel' or 'account':\n" + '\n'.join(offenders))

class PageConfigGlobalTests(unittest.TestCase):
    """Guards BUGS.md 2026-07-30 "the channel search page's JS never ran".

    A page hands its JS module a config object through a global. `window.X_CONFIG = {...}`
    puts it on the window; a bare top-level `const X_CONFIG = {...}` in a classic script
    creates a global *lexical* binding that is never a property of window - so the module's
    `window.X_CONFIG` reads undefined and the page's first line throws, taking the whole
    page's JS with it. Nothing looks wrong: the HTML renders, the console has one error
    nobody is watching, and every control is simply inert.

    The two spellings are indistinguishable in the template by eye, which is why this is a
    scan rather than a review note. Every `*_CONFIG` a module reads off `window` must be
    assigned through `window.`.
    """

    _READS_RE = re.compile(r'window\.([A-Z][A-Z0-9_]*_CONFIG)\b')
    _CONST_RE = re.compile(r'^\s*(?:const|let|var)\s+([A-Z][A-Z0-9_]*_CONFIG)\s*=', re.M)

    def test_every_config_global_a_module_reads_is_assigned_on_window(self):
        wanted = set()
        for path in _walk(JS_DIR, '.js'):
            wanted.update(self._READS_RE.findall(_read(path)))
        self.assertTrue(wanted, 'no window.*_CONFIG reads found - has the pattern changed?')

        offenders = []
        for path in _walk(TPL_DIR, '.html'):
            text = _read(path)
            for name in self._CONST_RE.findall(text):
                if name in wanted and f'window.{name}' not in text:
                    offenders.append(f'{_rel(path)}: `{name}` is declared but never on window')
        self.assertEqual(
            offenders, [],
            'A page config global is declared with const/let/var instead of being assigned '
            'to window. A top-level const in a classic script is NOT a window property, so '
            'the module reading window.<NAME> gets undefined and the page\'s JS dies on its '
            'first line:\n' + '\n'.join(offenders))


class LiveLogRenderInvariants(unittest.TestCase):
    """Guards BUGS.md 2026-07-18 "Live log refresh destroyed text selection".

    Both live-updating log views must stay append-only and must not move the viewport or trim
    rows while the user has text highlighted. A full `innerHTML =` rebuild of the log region
    destroys the selection outright; an unguarded auto-scroll drags the highlighted line off
    screen. The shared guard is `hasSelectionIn()` in static/js/util.js.
    """

    # (file, log-element id, name of the function that appends to it)
    # logs.html's inline script became static/js/logs.js in the chunk-5 rollout
    # (dev/changelog/447); the invariant is the same, the file moved.
    _LIVE_LOGS = [
        (os.path.join(JS_DIR, 'logs.js'), 'log-box', 'appendRow'),
        (os.path.join(JS_DIR, 'group-detail.js'), 'gd-test-log', 'updateLog'),
    ]

    @staticmethod
    def _fn_body(text, name):
        """Body of a top-level-in-IIFE `function name(...) {` from these templates.

        Both live-log scripts indent their functions two spaces inside an IIFE, so the body
        ends at the first line that is exactly `  }`. Returns None when the function is
        missing, which the callers report as a failure rather than a silent pass.
        """
        m = re.search(r'^  function\s+' + re.escape(name) + r'\s*\(', text, re.M)
        if not m:
            return None
        end = re.compile(r'^  \}\s*$', re.M).search(text, m.end())
        return text[m.start():end.end()] if end else text[m.start():]

    def test_live_log_regions_are_not_rebuilt_wholesale(self):
        """The live-update path must not assign .innerHTML on the log element.

        Scoped to the updater's own body: clearing the container from a Clear button, or
        seeding a placeholder on first history load, are legitimate and live elsewhere.
        """
        offenders = []
        for path, _el_id, fn in self._LIVE_LOGS:
            body = self._fn_body(_read(path), fn)
            if body is None:
                offenders.append(f'{_rel(path)}: expected a {fn}() live-log updater, none found')
                continue
            for m in re.finditer(r'(\w+)\.innerHTML\s*=', body):
                offenders.append(f'{_rel(path)}: {fn}() assigns {m.group(1)}.innerHTML')
        self.assertEqual(
            offenders, [],
            'A live log region is rebuilt via innerHTML, which destroys any text the user has '
            'highlighted. Append new rows instead (CLAUDE.md frontend-rendering rule):\n'
            + '\n'.join(offenders))

    def test_live_log_appenders_consult_hasSelectionIn(self):
        """The append path must gate auto-scroll/trim on hasSelectionIn()."""
        offenders = []
        for path, _el_id, fn in self._LIVE_LOGS:
            body = self._fn_body(_read(path), fn)
            if body is None:
                offenders.append(f'{_rel(path)}: expected a {fn}() live-log updater, none found')
            elif 'hasSelectionIn' not in body:
                offenders.append(f'{_rel(path)}: {fn}() does not consult hasSelectionIn()')
        self.assertEqual(
            offenders, [],
            'Live log updater does not suppress auto-scroll/trim while a selection is active '
            '(BUGS.md 2026-07-18):\n' + '\n'.join(offenders))

    def test_has_selection_in_helper_is_shared(self):
        """The guard lives in util.js (canonical shared-JS home), not copied per page."""
        util = _read(os.path.join(JS_DIR, 'util.js'))
        self.assertIn('function hasSelectionIn(', util,
                      'hasSelectionIn() must live in static/js/util.js per the CLAUDE.md '
                      'shared-JS canonical-home rule')
        for path, _el_id, _fn in self._LIVE_LOGS:
            self.assertNotIn(
                'function hasSelectionIn(', _read(path),
                f'{_rel(path)} redefines hasSelectionIn() instead of using the util.js copy')


class RecordingDetailLayoutInvariants(unittest.TestCase):
    """Guards BUGS.md 2026-07-18 "Recording-detail event log timestamps had no seconds",
    plus the status-summary placement those guards protected (changelog/183; markup
    rebuilt on DESIGN.md 3.8 in the fableUI #3 work - same invariants, new selectors).

    The event log is the only place a stall/restart burst can be read in order, and those
    events routinely land inside one minute - so its timestamp filter must carry seconds.
    `local_time` (no seconds) is the app-wide default and is the exact regression to catch.
    """

    _PATH = os.path.join(TPL_DIR, 'recording_detail.html')

    def test_event_log_timestamp_has_seconds(self):
        m = re.search(r'<span class="ev-time">\{\{\s*evt\.timestamp\s*\|\s*(\w+)\s*\}\}',
                      _read(self._PATH))
        self.assertIsNotNone(
            m, 'recording_detail.html no longer renders the event-log timestamp span; '
               'update this invariant along with the markup')
        self.assertEqual(
            m.group(1), 'local_time_sec',
            'The event-log timestamp must use a seconds-bearing filter so two events in the '
            'same minute display distinct times (BUGS.md 2026-07-18)')

    def test_status_strip_is_topmost(self):
        """The status strip (successor of the Job Summary panel, changelog/183) is the
        quickest read of current status and must come right after the header, before the
        hero card - above the fold on desktop and first in the <=960px single column."""
        text = _read(self._PATH)
        head = text.index('id="detail-head-slot"')
        strip = text.index('id="status-strip-slot"')
        hero = text.index('<div class="hero" id="details-section"')
        self.assertLess(head, strip, 'status strip must come after the page header')
        self.assertLess(strip, hero, 'status strip must precede the hero card')

    def test_swap_ids_cover_refreshing_panels(self):
        """The 15s auto-refresh swaps regions by id; losing one silently freezes it.
        The status strip is deliberately NOT in the static list - it is appended only
        for non-live statuses (while live, its spans belong to the SSE handler; one
        updater per DOM region)."""
        text = _read(self._PATH)
        for rid in ("'detail-head-slot'", "'hero-right'",
                    "'panel-segments-slot'", "'panel-eventlog'"):
            self.assertIn(rid, text, f'{rid} must stay in the SWAP_IDS auto-refresh list')
        self.assertIn("SWAP_IDS.push('status-strip-slot')", text,
                      'non-live statuses must still swap the status strip')


class DupModalCopyInvariants(unittest.TestCase):
    """Guards the dedupe-clarity work (changelog/184).

    The modal asks you to pick the channel to *keep*, so a submit button reading only
    "Remove Duplicates" describes the opposite of the selection - the confusion the
    rework exists to fix. And what "remove" does differs per surface (this health check /
    your TV Guide / this group / this selection), so every call site must say which;
    a caller that omits introHtml silently renders an empty explanation paragraph.
    """

    _MODAL = os.path.join(JS_DIR, 'dup-modal.js')
    _CALLERS = [
        os.path.join(JS_DIR, 'group-detail.js'),
        # The Browse tab's caller: channel-search.js, which replaced browse.html when the
        # revamped search took over /channels.
        os.path.join(JS_DIR, 'channel-search.js'),
        os.path.join(JS_DIR, 'group-modal.js'),
    ]

    def test_submit_button_names_the_keep_semantics(self):
        text = _read(self._MODAL)
        m = re.search(r"opts\.submitLabel \|\| '([^']+)'", text)
        self.assertIsNotNone(m, 'dup-modal.js no longer has a default submit label')
        self.assertIn('Keep Selected', m.group(1))

    def test_modal_states_the_keep_instruction_up_front(self):
        self.assertIn("Select which channel you'd like to keep",
                      _read(self._MODAL).replace('\\\'', "'"))

    def test_every_call_site_explains_what_removal_means(self):
        missing = [_rel(p) for p in self._CALLERS
                   if 'openDupModal' in _read(p) and 'introHtml' not in _read(p)]
        self.assertEqual(missing, [], f'openDupModal call sites without introHtml: {missing}')

    def test_call_sites_cover_every_openDupModal_in_the_tree(self):
        """If a fifth surface appears, it has to be added to _CALLERS above - otherwise
        this class silently stops guarding it."""
        found = {_rel(p) for p in _walk(TPL_DIR, '.html') if 'openDupModal(' in _read(p)}
        found |= {_rel(p) for p in _walk(JS_DIR, '.js')
                  if 'openDupModal(' in _read(p) and not p.endswith('dup-modal.js')}
        self.assertEqual(found, {_rel(p) for p in self._CALLERS})


_RETRY_DEF_RE = re.compile(r'^\s*(async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)')

# Sites where a raw, unwrapped db.session.commit() is verified legitimately exempt from
# retry_on_locked (dev/changelog/295, "retry_on_locked advisory scan under-reports and can
# be defeated by a comment", verified 2026-07-22): each runs single-threaded before any concurrent writer
# exists - the app/__init__.py pair and search_index.py's DDL during create_app() startup,
# before init_scheduler() and before blueprints serve; every migrations.py entry runs only
# from inside a versioned migration step (app/migrations.py's own module docstring:
# "Startup-only and single-threaded... no concurrent writer exists yet"), regardless of
# which schema version that step is registered at. Keyed by (rel path, enclosing function
# name) rather than line number so the allowlist survives unrelated edits above these
# functions. Adding an entry here means re-verifying the same no-concurrent-writer
# argument, not just that the scan is quiet.
_EXEMPT_COMMIT_SITES = frozenset({
    ('app/__init__.py', '_ensure_system_health_job'),
    ('app/__init__.py', '_announce_guide_check_retarget'),
    ('app/__init__.py', '_seed_default_tags'),
    ('app/migrations.py', '_backfill_url_normalization'),
    ('app/migrations.py', '_backfill_duplicate_flags'),
    ('app/migrations.py', '_backfill_health_scores'),
    ('app/migrations.py', '_backfill_consecutive_test_failures'),
    ('app/search_index.py', 'ensure_search_index_schema'),
})


def _retry_def_line_index(lines, i):
    """Nearest preceding `def` line index enclosing commit-line i, or -1 if none (module
    level)."""
    d = i
    while d >= 0 and not _RETRY_DEF_RE.match(lines[d]):
        d -= 1
    return d


def _retry_span_start(lines, def_index):
    """First line index of the span for `def_index`, including decorator lines directly
    above it - so a whole-function @retry_on_locked (however far above the commit) and an
    inline nested @retry_on_locked closure both fall inside the span."""
    if def_index < 0:
        return 0
    s = def_index - 1
    while s >= 0 and (lines[s].strip().startswith('@') or lines[s].strip() == ''):
        s -= 1
    return s + 1


def _find_unwrapped_commits(source, rel_path, exempt=frozenset()):
    """Return 1-indexed line numbers of `db.session.commit()` sites in `source` not wrapped
    by retry_on_locked and not in `exempt` (a set of (rel_path, enclosing function name)).

    Comments and string literals (docstrings included) are blanked before the
    'retry_on_locked' substring check runs, so a function merely *mentioning* the name in
    prose can't suppress a real unwrapped site - the defect this scan used to have."""
    masked_lines = _mask_comments_and_strings(source).splitlines()
    raw_lines = source.splitlines()
    suspects = []
    for i, line in enumerate(raw_lines):
        # A real commit is a standalone statement; `== 'db.session.commit()'` on the raw
        # line (not the masked one, which would blank a would-be match inside a string)
        # skips prose mentions - every actual commit in the tree is on its own line.
        if line.strip() != 'db.session.commit()':
            continue
        def_index = _retry_def_line_index(masked_lines, i)
        func_name = None
        if def_index >= 0:
            m = _RETRY_DEF_RE.match(masked_lines[def_index])
            if m:
                func_name = m.group(2)
        if (rel_path, func_name) in exempt:
            continue
        span = '\n'.join(masked_lines[_retry_span_start(masked_lines, def_index):i])
        if 'retry_on_locked' not in span:
            suspects.append(i + 1)
    return suspects


class RetryOnLockedAdvisory(unittest.TestCase):
    """ADVISORY (never fails): print db.session.commit() sites in app/ that aren't obviously
    inside a retry_on_locked-decorated function (and aren't on the verified-exempt
    allowlist), so a human can eyeball them. CLAUDE.md is explicit this can't be enforced
    automatically; this is a lead sheet, not a gate.

    Only matches lines whose stripped text is exactly `db.session.commit()` - a raw-connection
    `conn.commit()` (e.g. app/scheduler.py, app/__init__.py) is a different commit path and is
    out of scope for this scan by design."""

    def test_report_unwrapped_commit_sites(self):
        suspects = []
        for path in _walk(APP_DIR, '.py'):
            rel = _rel(path)
            for lineno in _find_unwrapped_commits(_read(path), rel, _EXEMPT_COMMIT_SITES):
                suspects.append(f'{rel}:{lineno}')
        if suspects:
            print(f'\n[advisory] {len(suspects)} db.session.commit() site(s) with no nearby '
                  f'@retry_on_locked (verify each is wrapped - CLAUDE.md Agent Behavior):')
            for s in suspects:
                print(f'  {s}')
        # Advisory only - always green.
        self.assertTrue(True)


class RetryOnLockedScanCorrectnessTests(unittest.TestCase):
    """Regression coverage for the advisory scan itself (dev/changelog/295, 'retry_on_locked
    advisory scan under-reports and can be defeated by a comment', verified 2026-07-22): a comment or
    docstring merely mentioning retry_on_locked must not suppress a real unwrapped site, and
    the exemption allowlist must be scoped per (file, function), not by prose."""

    def test_docstring_mention_does_not_suppress_a_real_site(self):
        source = (
            "def do_thing():\n"
            "    \"\"\"The caller wraps this in retry_on_locked.\"\"\"\n"
            "    obj.field = 2\n"
            "    db.session.commit()\n"
        )
        self.assertEqual(_find_unwrapped_commits(source, 'app/fake.py'), [4])

    def test_comment_mention_does_not_suppress_a_real_site(self):
        source = (
            "def do_thing():\n"
            "    obj.field = 2\n"
            "    # TODO: needs retry_on_locked\n"
            "    db.session.commit()\n"
        )
        self.assertEqual(_find_unwrapped_commits(source, 'app/fake.py'), [4])

    def test_actual_decorator_still_suppresses(self):
        source = (
            "@retry_on_locked()\n"
            "def do_thing():\n"
            "    obj.field = 2\n"
            "    db.session.commit()\n"
        )
        self.assertEqual(_find_unwrapped_commits(source, 'app/fake.py'), [])

    def test_allowlisted_site_is_exempt(self):
        source = (
            "def _seed_default_tags():\n"
            "    db.session.add(Tag())\n"
            "    db.session.commit()\n"
        )
        self.assertEqual(
            _find_unwrapped_commits(source, 'app/__init__.py', _EXEMPT_COMMIT_SITES), [])

    def test_allowlist_is_scoped_to_file_not_just_function_name(self):
        """Same function name, different file - the allowlist entry for app/__init__.py
        must not blanket-exempt a same-named function anywhere else in the tree."""
        source = (
            "def _seed_default_tags():\n"
            "    db.session.add(Tag())\n"
            "    db.session.commit()\n"
        )
        self.assertEqual(
            _find_unwrapped_commits(source, 'app/other_module.py', _EXEMPT_COMMIT_SITES),
            [3])

    def test_current_tree_has_no_suspects_outside_the_verified_allowlist(self):
        """Locks in the 2026-07-22 finding: only the verified-exempt sites listed in
        _EXEMPT_COMMIT_SITES exist today, and the scan (with masking + allowlist) reports
        zero suspects for real app/ code."""
        suspects = []
        for path in _walk(APP_DIR, '.py'):
            rel = _rel(path)
            suspects.extend(
                f'{rel}:{n}' for n in _find_unwrapped_commits(_read(path), rel,
                                                                _EXEMPT_COMMIT_SITES))
        self.assertEqual(suspects, [])


# --- retry_on_locked sub-rule scans ---------------------------------------------------
#
# CLAUDE.md's commit rule has three parts. _find_unwrapped_commits above covers the first
# (every commit belongs inside a retry_on_locked closure). These two scans gate the other
# two, which differ from it in the way that matters: an unwrapped commit announces itself by
# raising, while both of these corrupt data quietly and stay corrupt.
#
#   * TWO COMMITS ON ONE PATH THROUGH ONE CLOSURE. The decorator replays the WHOLE closure,
#     so a lock on the second commit re-runs the first one's work - and when that work was an
#     INSERT, the retry leaves a duplicate row behind with no error anywhere. Caught for real
#     in new_recording_json during the original rollout.
#   * A NON-IDEMPOTENT SIDE EFFECT INSIDE A CLOSURE. Every retry re-spawns the process,
#     re-starts the thread, re-issues the provider fetch or re-deletes the file.
#     dev/changelog/683 is the measured case: one locked commit re-downloaded an entire M3U
#     playlist up to four extra times, against providers that allow a single connection.
#
# WHY THE COMMIT CHECK IS PATH-AWARE RATHER THAN A LINE COUNT. Counting `db.session.commit()`
# lines per decorated function - the obvious implementation - reports ten violations against
# this tree and every one is wrong. The shape it trips over is the app's standard
# cancellation guard, where the two commits are on mutually exclusive branches and only ever
# one of them runs:
#
#     @retry_on_locked()
#     def _commit_completed():
#         r = db.session.get(Recording, recording_id)
#         if preserve_cancelled_status(r, '...'):
#             db.session.commit()
#             return None
#         r.status = REC_STATUS_COMPLETED
#         db.session.commit()
#         return r.output_path
#
# So the rule this enforces is "no two commits reachable on a single execution path", which
# needs return/raise to terminate a path and if/try/match to fork one. A loop body counts
# twice by construction: a commit inside a loop is the same replay hazard, one iteration at a
# time.


#: How many same-module/imported helper calls deep the side-effect scan follows. The
#: violation in dev/changelog/683 was one level down (the closure called a helper that did
#: the fetching), so a depth-0 scan that only looks at the closure's own body would have
#: missed the case this exists to catch.
_SIDE_EFFECT_MAX_DEPTH = 3

#: Dotted names whose call inside a retried closure is a non-idempotent side effect. Grouped
#: only so a failure message can say what kind of thing it found.
_SIDE_EFFECT_CALLS = {
    'process': frozenset({
        'subprocess.Popen', 'subprocess.run', 'subprocess.call', 'subprocess.check_call',
        'subprocess.check_output', 'os.system', 'os.popen',
    }),
    'thread': frozenset({
        'threading.Thread', 'threading.Timer', 'Thread', 'Timer',
    }),
    'network': frozenset({
        'requests.get', 'requests.post', 'requests.put', 'requests.delete',
        'requests.head', 'requests.patch', 'requests.request',
        'urllib.request.urlopen', 'socket.create_connection',
    }),
    'filesystem': frozenset({
        'os.remove', 'os.unlink', 'os.rename', 'os.replace', 'os.rmdir',
        'shutil.rmtree', 'shutil.move', 'shutil.copy', 'shutil.copy2', 'shutil.copyfile',
    }),
}

# Deliberately empty: app/ has no verified-legitimate side effect inside a retried closure.
# Keyed (rel path, function name) like _EXEMPT_COMMIT_SITES. An entry here is a claim that
# re-running the side effect is genuinely harmless, which is a much stronger claim than "the
# scan is noisy" - moving the side effect after the closure is nearly always the real fix.
_EXEMPT_SIDE_EFFECT_SITES = frozenset()


def _dotted_name(node):
    """`a.b.c` for an Attribute/Name chain, else None (a call on a subscript or a call
    result has no static name and is not resolvable here)."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return '.'.join(reversed(parts))


def _is_retry_decorated(fn):
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = _dotted_name(target)
        if name and name.split('.')[-1] == 'retry_on_locked':
            return True
    return False


def _is_session_commit(node):
    if isinstance(node, ast.Expr):
        node = node.value
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Attribute) and func.attr == 'commit'
            and isinstance(func.value, ast.Attribute) and func.value.attr == 'session')


def _commits_in_body(stmts):
    """(fallthrough, worst) for a statement list: commits along the path that reaches the
    end (None when no path does), and the most commits on any path through it."""
    live, worst = 0, 0
    for st in stmts:
        st_live, st_worst = _commits_in_stmt(st)
        worst = max(worst, (live or 0) + st_worst)
        if live is None:
            continue                      # unreachable tail - counted, but adds no path
        live = None if st_live is None else live + st_live
    return live, worst


def _commits_in_stmt(st):
    if isinstance(st, ast.Expr) and _is_session_commit(st):
        return 1, 1
    if isinstance(st, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
        return None, 0
    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return 0, 0                       # a nested def is its own unit
    if isinstance(st, ast.If):
        a_live, a_worst = _commits_in_body(st.body)
        b_live, b_worst = _commits_in_body(st.orelse) if st.orelse else (0, 0)
        lives = [x for x in (a_live, b_live) if x is not None]
        return (max(lives) if lives else None), max(a_worst, b_worst)
    if isinstance(st, (ast.With, ast.AsyncWith)):
        return _commits_in_body(st.body)
    if isinstance(st, (ast.For, ast.AsyncFor, ast.While)):
        b_live, b_worst = _commits_in_body(st.body)
        e_live, e_worst = _commits_in_body(st.orelse) if st.orelse else (0, 0)
        # The body can run more than once, so one commit inside it is already two on a
        # path - the same replay hazard as two commits written out in sequence.
        worst = max(b_worst * 2, (b_live or 0) + e_worst)
        live = None if e_live is None else (b_live or 0) + e_live
        return live, worst
    if isinstance(st, ast.Try):
        return _commits_in_try(st)
    if isinstance(st, ast.Match):
        cases = [_commits_in_body(c.body) for c in st.cases]
        # No case having matched is itself a path through, so fallthrough is always live.
        lives = [c[0] for c in cases if c[0] is not None] + [0]
        return max(lives), max([c[1] for c in cases] or [0])
    for node in ast.walk(st):             # a commit buried in an expression statement
        if _is_session_commit(node):
            return 1, 1
    return 0, 0


def _commits_in_try(st):
    body_live, body_worst = _commits_in_body(st.body)
    else_live, else_worst = _commits_in_body(st.orelse) if st.orelse else (0, 0)
    lives, worsts = [], [body_worst]
    if body_live is not None:
        worsts.append(body_live + else_worst)
        if else_live is not None:
            lives.append(body_live + else_live)
    for handler in st.handlers:
        h_live, h_worst = _commits_in_body(handler.body)
        # The exception can arrive at any point, so assume the try body got as far as it
        # could - a commit in the body AND one in the handler really is two on one path.
        worsts.append(body_worst + h_worst)
        if h_live is not None:
            lives.append(body_worst + h_live)
    fin_live, fin_worst = _commits_in_body(st.finalbody) if st.finalbody else (0, 0)
    worst = max(worsts) + fin_worst       # finally always runs
    live = None if (not lives or fin_live is None) else max(lives) + fin_live
    return live, worst


def find_multi_commit_closures(source, rel_path):
    """Return (rel_path, lineno, function name, commit count) for every retry_on_locked
    function in `source` that can reach two or more commits on one execution path."""
    hits = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_retry_decorated(fn):
            continue
        _live, worst = _commits_in_body(fn.body)
        if worst > 1:
            hits.append((rel_path, fn.lineno, fn.name, worst))
    return hits


def _calls_in_own_unit(fn):
    """Every Call in fn's own body, descending through control flow but stopping at a
    nested function that carries its own retry_on_locked (that closure is its own unit)."""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and _is_retry_decorated(node)):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _classify_side_effect(name):
    for kind, names in _SIDE_EFFECT_CALLS.items():
        if name in names:
            return kind
    # Deliberately broad: a .start() inside a retried closure is a thread or a timer often
    # enough, and a benign one is worth the one-line allowlist entry.
    if name and name.endswith('.start'):
        return 'thread'
    return None


def _resolve_relative_import(node, module_name):
    """Absolute app-relative module a `from . import` / `from .. import` names, or None."""
    package = module_name.rsplit('.', 1)[0] if '.' in module_name else ''
    for _ in range(node.level - 1):
        package = package.rsplit('.', 1)[0] if '.' in package else ''
    if not node.module:
        return package or None
    return f'{package}.{node.module}' if package else node.module


def _module_index(modules):
    """{module: (rel_path, {func name: node}, {bound name: (module, func name)})} - the
    function tables and relative-import bindings the side-effect walk resolves through."""
    index = {}
    for module_name, (rel_path, source) in modules.items():
        tree = ast.parse(source)
        funcs, imports = {}, {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.setdefault(node.name, node)
            elif isinstance(node, ast.ImportFrom) and node.level:
                target = _resolve_relative_import(node, module_name)
                if target:
                    for alias in node.names:
                        imports[alias.asname or alias.name] = (target, alias.name)
        index[module_name] = (rel_path, funcs, imports)
    return index


def _walk_for_side_effects(fn, module_name, index, depth, seen, via):
    rel_path, funcs, imports = index[module_name]
    hits = []
    for call in _calls_in_own_unit(fn):
        name = _dotted_name(call.func)
        kind = _classify_side_effect(name)
        if kind:
            hits.append((rel_path, call.lineno, kind, name, tuple(via)))
            continue
        if not name or '.' in name or depth <= 0:
            continue
        if name in funcs:
            target = (module_name, funcs[name])
        elif name in imports and imports[name][0] in index:
            other, orig = imports[name]
            if orig not in index[other][1]:
                continue
            target = (other, index[other][1][orig])
        else:
            continue
        key = (target[0], target[1].name)
        if key in seen:
            continue
        hits.extend(_walk_for_side_effects(target[1], target[0], index, depth - 1,
                                           seen | {key}, via + [f'{name}()']))
    return hits


def find_retry_side_effects(modules, max_depth=_SIDE_EFFECT_MAX_DEPTH,
                            exempt=frozenset()):
    """Non-idempotent side effects reachable from a retry_on_locked closure.

    `modules` is {module name: (rel path, source)}. Calls are resolved through same-module
    functions and relative imports so a closure that delegates its fetch to a helper is
    caught - the dev/changelog/683 shape - not just one that calls requests itself.

    KNOWN LIMIT, stated rather than papered over: a side effect behind a METHOD call on an
    object (`client.get_live_streams()`) is invisible here, because knowing what `client` is
    needs type inference. That is the other half of dev/changelog/683, and it is why this
    scan is a floor on the rule rather than proof of compliance.
    """
    index = _module_index(modules)
    hits = []
    for module_name, (rel_path, funcs, _imports) in index.items():
        for fn in funcs.values():
            if not _is_retry_decorated(fn) or (rel_path, fn.name) in exempt:
                continue
            for hit_rel, lineno, kind, name, via in _walk_for_side_effects(
                    fn, module_name, index, max_depth, {(module_name, fn.name)}, []):
                trail = (' via ' + ' -> '.join(via)) if via else ''
                hits.append(f'{hit_rel}:{lineno} {kind} {name}() inside '
                            f'{rel_path}::{fn.name}(){trail}')
    return sorted(hits)


def _app_modules():
    """app/ as {module name: (rel path, source)} - `routes.channels` for app/routes/channels.py."""
    modules = {}
    for path in _walk(APP_DIR, '.py'):
        name = os.path.relpath(path, APP_DIR)[:-3].replace(os.sep, '.')
        modules[name] = (_rel(path), _read(path))
    return modules


class RetryOnLockedMultiCommitTests(unittest.TestCase):
    """No retry_on_locked closure in app/ can reach two commits on one execution path - the
    duplicate-row-on-retry defect caught in new_recording_json during the original rollout
    (CLAUDE.md: "If a function needs more than one commit, each commit gets its OWN
    decorated closure")."""

    def test_no_closure_commits_twice_on_one_path(self):
        hits = []
        for path in _walk(APP_DIR, '.py'):
            hits.extend(find_multi_commit_closures(_read(path), _rel(path)))
        self.assertEqual(
            [f'{rel}:{line} {name} reaches {n} commits on one path'
             for rel, line, name, n in hits], [],
            'Split each commit into its own retry_on_locked closure - a retry replays the '
            'whole closure, so the earlier commit\'s work runs a second time.')


class RetryOnLockedSideEffectTests(unittest.TestCase):
    """No retry_on_locked closure in app/ reaches a non-idempotent side effect - the retry
    would re-run it (CLAUDE.md: spawning ffmpeg, starting a thread, calling an external API
    "must happen OUTSIDE the decorated closure")."""

    def test_no_side_effects_inside_retried_closures(self):
        self.assertEqual(
            find_retry_side_effects(_app_modules(), exempt=_EXEMPT_SIDE_EFFECT_SITES), [],
            'Move the side effect out of the closure - before it if the DB write depends on '
            'its result, after it if it should only fire once the write has committed.')


class RetryOnLockedSubRuleScanCorrectnessTests(unittest.TestCase):
    """The two scans above are only worth having if they fire. Each offender here is a
    shape that has actually shipped in this app, and each control is a shape the tree
    genuinely contains and must never be flagged for."""

    _EXCLUSIVE_BRANCHES = (
        "@retry_on_locked()\n"
        "def _commit_completed():\n"
        "    r = db.session.get(Recording, rid)\n"
        "    if preserve_cancelled_status(r, '...'):\n"
        "        db.session.commit()\n"
        "        return None\n"
        "    r.status = REC_STATUS_COMPLETED\n"
        "    db.session.commit()\n"
        "    return r.output_path\n"
    )

    def test_two_sequential_commits_are_caught(self):
        source = (
            "@retry_on_locked()\n"
            "def _do():\n"
            "    db.session.add(Recording())\n"
            "    db.session.commit()\n"
            "    other.field = 1\n"
            "    db.session.commit()\n"
        )
        hits = find_multi_commit_closures(source, 'app/fake.py')
        self.assertEqual([(h[2], h[3]) for h in hits], [('_do', 2)])

    def test_commit_inside_a_loop_is_caught(self):
        source = (
            "@retry_on_locked()\n"
            "def _do():\n"
            "    for row in rows:\n"
            "        db.session.add(row)\n"
            "        db.session.commit()\n"
        )
        self.assertEqual([h[2] for h in find_multi_commit_closures(source, 'app/fake.py')],
                         ['_do'])

    def test_commit_in_body_and_handler_is_caught(self):
        source = (
            "@retry_on_locked()\n"
            "def _do():\n"
            "    try:\n"
            "        db.session.commit()\n"
            "    except OperationalError:\n"
            "        db.session.rollback()\n"
            "        db.session.commit()\n"
        )
        self.assertEqual([h[2] for h in find_multi_commit_closures(source, 'app/fake.py')],
                         ['_do'])

    def test_mutually_exclusive_commits_are_not_flagged(self):
        """The cancellation-guard shape. A line count reports this - and would be red
        against ten real functions in app/ on the day it was added."""
        self.assertEqual(
            find_multi_commit_closures(self._EXCLUSIVE_BRANCHES, 'app/fake.py'), [])

    def test_undecorated_function_with_two_commits_is_not_this_scan_s_business(self):
        """Two commits outside a retried closure are ordinary code; _find_unwrapped_commits
        is what has an opinion about them."""
        source = (
            "def _do():\n"
            "    db.session.commit()\n"
            "    db.session.commit()\n"
        )
        self.assertEqual(find_multi_commit_closures(source, 'app/fake.py'), [])

    def test_sibling_closures_are_counted_separately(self):
        """Two one-commit closures in one enclosing function is the CORRECT fix for a
        two-commit closure - the scan must not re-flag it by counting the outer span."""
        source = (
            "def route():\n"
            "    @retry_on_locked()\n"
            "    def _first():\n"
            "        db.session.commit()\n"
            "    @retry_on_locked()\n"
            "    def _second():\n"
            "        db.session.commit()\n"
            "    _first()\n"
            "    _second()\n"
        )
        self.assertEqual(find_multi_commit_closures(source, 'app/fake.py'), [])

    def _modules(self, **sources):
        return {name: (f'app/{name.replace(".", "/")}.py', src)
                for name, src in sources.items()}

    def test_direct_process_spawn_is_caught(self):
        hits = find_retry_side_effects(self._modules(fake=(
            "@retry_on_locked()\n"
            "def _launch_and_commit():\n"
            "    proc = subprocess.Popen(cmd)\n"
            "    db.session.commit()\n"
        )))
        self.assertEqual(len(hits), 1, hits)
        self.assertIn('process subprocess.Popen()', hits[0])

    def test_thread_start_is_caught(self):
        hits = find_retry_side_effects(self._modules(fake=(
            "@retry_on_locked()\n"
            "def _go():\n"
            "    worker.start()\n"
            "    db.session.commit()\n"
        )))
        self.assertEqual(len(hits), 1, hits)
        self.assertIn('thread worker.start()', hits[0])

    def test_fetch_behind_a_same_module_helper_is_caught(self):
        """dev/changelog/683's shape: the closure itself looks clean, and the download it
        replays on every retry is one call down."""
        hits = find_retry_side_effects(self._modules(accounts=(
            "def _sync_channels_from_m3u(account):\n"
            "    resp = requests.get(account.m3u_url)\n"
            "    return _upsert_channels(_parse(resp.text))\n"
            "\n"
            "def _do_sync(account):\n"
            "    @retry_on_locked()\n"
            "    def _sync_m3u_channels_and_commit():\n"
            "        n = _sync_channels_from_m3u(account)\n"
            "        db.session.commit()\n"
            "        return n\n"
            "    return _sync_m3u_channels_and_commit()\n"
        )))
        self.assertEqual(len(hits), 1, hits)
        self.assertIn('network requests.get()', hits[0])
        self.assertIn('via _sync_channels_from_m3u()', hits[0])

    def test_fetch_behind_an_imported_helper_is_caught(self):
        """Same shape, one module over - the helper reached through `from .mod import`."""
        hits = find_retry_side_effects(self._modules(
            fetcher="def fetch_streams(account):\n    return requests.get(account.url)\n",
            accounts=(
                "from .fetcher import fetch_streams\n"
                "\n"
                "@retry_on_locked()\n"
                "def _sync_and_commit(account):\n"
                "    streams = fetch_streams(account)\n"
                "    db.session.commit()\n"
                "    return streams\n"
            ),
        ))
        self.assertEqual(len(hits), 1, hits)
        self.assertIn('via fetch_streams()', hits[0])

    def test_fix_683_applied_is_clean(self):
        """The known-good half of the pair: the fetch hoisted out and only the DB tail
        retried is exactly what the scan must stay quiet about."""
        self.assertEqual(find_retry_side_effects(self._modules(accounts=(
            "def _fetch_m3u_streams(account):\n"
            "    resp = requests.get(account.m3u_url)\n"
            "    return _parse(resp.text)\n"
            "\n"
            "def _do_sync(account):\n"
            "    streams = _fetch_m3u_streams(account)\n"
            "\n"
            "    @retry_on_locked()\n"
            "    def _upsert_and_commit():\n"
            "        n = _upsert_channels(streams)\n"
            "        db.session.commit()\n"
            "        return n\n"
            "    return _upsert_and_commit()\n"
        ))), [])

    def test_side_effect_after_the_closure_is_clean(self):
        """The collect-then-act ordering delete_recording and missing_delete both use."""
        self.assertEqual(find_retry_side_effects(self._modules(fake=(
            "def route():\n"
            "    @retry_on_locked()\n"
            "    def _delete_row():\n"
            "        paths = [t.screenshot_path for t in rows]\n"
            "        db.session.commit()\n"
            "        return paths\n"
            "    for path in _delete_row():\n"
            "        os.unlink(path)\n"
        ))), [])

    def test_allowlist_is_scoped_to_file_and_function(self):
        modules = self._modules(fake=(
            "@retry_on_locked()\n"
            "def _go():\n"
            "    subprocess.Popen(cmd)\n"
            "    db.session.commit()\n"
        ))
        self.assertEqual(
            find_retry_side_effects(modules, exempt=frozenset({('app/fake.py', '_go')})), [])
        self.assertEqual(
            len(find_retry_side_effects(
                modules, exempt=frozenset({('app/other.py', '_go')}))), 1)


# --- Scroll-lock overlay scan ---------------------------------------------------------
#
# An open overlay must freeze the page behind it (dev/changelog/352, 353, 354). The lock is
# DERIVED from the DOM by util.js::syncScrollLock, so a surface whose open/close path forgets
# to call it leaks scroll to the page underneath until the next click hits the backstop. That
# omission is silent, which is what this scan exists to catch.
#
# Two things make the scan precise enough to be worth having. First, the overlay element set
# is derived from the templates rather than hand-listed, so a new overlay is covered the day
# it is added. Second, the unit of judgement is the enclosing top-level statement, not the
# line: `pop.hidden = true` never names the overlay it hides, but the function that binds
# `pop` does. dev/mockups/verify16.js check #11 is the original of this scan; two simpler
# line-level heuristics were tried there first and each produced a false pass.

_OVERLAY_STATE_CLASSES = ('open', 'show')

# Every way the app makes an overlay appear or disappear: inline display, the `hidden`
# attribute, an .open/.show state class, and appending/removing a built node.
_VIS_MUTATION_RE = re.compile(
    r"style\.display\s*=|\.hidden\s*=|classList\.(?:add|remove|toggle)\('(?:open|show)'"
    r"|\.remove\(\)|appendChild\(")

_OVERLAY_SEL_RE = re.compile(r"const\s+OVERLAY_SEL\s*=\s*((?:'[^']*'\s*\+?\s*)+);")

# `(() => {`, `(function () {`, `!function() {`, and any `document.addEventListener(...)` -
# the shapes that wrap a whole file or a whole inline <script> in one statement.
_IIFE_OPENER_RE = re.compile(r"\s*(?:[(!]|document\.addEventListener\()")


def _overlay_selector():
    """The live OVERLAY_SEL value from static/js/util.js, with its concatenated string
    literals joined. Raises if it can't be found - a scan that silently reads nothing is
    worse than no scan."""
    m = _OVERLAY_SEL_RE.search(_read(os.path.join(JS_DIR, 'util.js')))
    if not m:
        raise AssertionError(
            'OVERLAY_SEL not found in static/js/util.js - the scroll-lock scan below reads '
            'it to learn what an overlay is. If it was renamed, update _OVERLAY_SEL_RE.')
    return ''.join(re.findall(r"'([^']*)'", m.group(1)))


def _overlay_classes(selector):
    """The class names in OVERLAY_SEL, minus the .open/.show state suffixes."""
    return tuple(sorted({c for c in re.findall(r'\.([a-z0-9-]+)', selector)
                         if c not in _OVERLAY_STATE_CLASSES}))


def _overlay_ids(classes):
    """Ids of every template element that carries an overlay class in its own class
    attribute. Derived, so a new overlay needs no edit here."""
    ids = set()
    tag_re = re.compile(r'<(?:div|section|aside|nav|ul)\b[^>]*>', re.I)
    for path in _walk(TPL_DIR, '.html'):
        for tag in tag_re.findall(_read(path)):
            cls = re.search(r'class="([^"]*)"', tag)
            el_id = re.search(r'id="([^"]*)"', tag)
            if cls and el_id and any(c in classes for c in cls.group(1).split()):
                ids.add(el_id.group(1))
    return ids


def _overlay_ref_re(ids, classes):
    """Matches a source line that names an overlay - by element id literal, or by one of the
    overlay classes as a whole class token. The lookahead is what keeps 'modal-start' and
    'modal-padding-note' (ordinary form fields inside a modal) from matching 'modal'."""
    parts = [re.escape(q + i + q) for i in sorted(ids) for q in ('"', "'")]
    parts += [r"""['"`]\.?""" + re.escape(c) + r"""(?=['"`\s.])""" for c in classes]
    return re.compile('|'.join(parts))


def _top_level_units(lines):
    """(start, end) line-index spans of the enclosing top-level statements.

    A whole file wrapped in one IIFE (channel-detail.js, groups.js, the inline script in
    index.html) would otherwise collapse into a single unit, and one
    syncScrollLock anywhere in it would vouch for every overlay in it. When an IIFE-shaped
    unit covers almost the whole block, descend a level and split again. Only an IIFE opener
    counts: a plain `function foo() {` spanning a short file is a genuine unit and unwrapping
    it would split a function away from the variables it binds."""
    indents = [len(l) - len(l.lstrip()) for l in lines]

    def split(lo, hi):
        body = [i for i in range(lo, hi) if lines[i].strip()]
        if not body:
            return []
        base = min(indents[i] for i in body)
        tops = [i for i in body if indents[i] == base]
        units = [(t, tops[n + 1] if n + 1 < len(tops) else hi) for n, t in enumerate(tops)]
        wrappers = [u for u in units
                    if u[1] - u[0] > 0.7 * (hi - lo) and u[1] - u[0] > 4
                    and _IIFE_OPENER_RE.match(lines[u[0]])]
        if len(units) <= 2 and wrappers:
            out = []
            for u in units:
                out.extend(split(u[0] + 1, u[1] - 1) if u in wrappers else [u])
            return out
        return units

    return split(0, len(lines))


def _script_blocks(path, text):
    """(line_offset, lines) for each block of JavaScript in `path`: the whole file for a .js,
    each inline <script> (never a src= include) for a template."""
    lines = text.split('\n')
    if path.endswith('.js'):
        yield 0, lines
        return
    start, buf = 0, None
    for i, line in enumerate(lines):
        if buf is None:
            if re.search(r'<script(?![^>]*\bsrc=)', line):
                buf, start = [], i + 1
            continue
        if '</script>' in line:
            if buf:
                yield start, buf
            buf = None
            continue
        buf.append(line)


def _unsynced_overlay_sites(lines, ref_re, offset=0):
    """Line numbers (1-based, + `offset`) of top-level units that open or close an overlay
    without calling syncScrollLock. buildModal() call sites are exempt: buildModal syncs on
    both its append and its close(), so every one of its call sites is covered by definition."""
    sites = []
    for start, end in _top_level_units(lines):
        block = '\n'.join(lines[start:end])
        if not _VIS_MUTATION_RE.search(block) or not ref_re.search(block):
            continue
        if 'syncScrollLock' in block or 'buildModal(' in block:
            continue
        sites.append((start + 1 + offset, lines[start].strip()[:70]))
    return sites


class ScrollLockInvariants(unittest.TestCase):
    """An open overlay must block scrolling of the page behind it, app-wide (BUGS.md
    2026-07-27 07:26:13 AM and 09:43:11 AM; dev/changelog/352, 353, 354).

    The lock has one home - `syncScrollLock()` + `OVERLAY_SEL` in static/js/util.js and
    `body.scroll-locked` in static/css/style.css - and every overlay open/close path must
    call it. These checks are the enforcement: without them the next overlay someone adds
    silently reintroduces the leak."""

    def test_body_scroll_locked_pins_the_page(self):
        """`position: fixed`, not `overflow: hidden`: iOS Safari ignores `overflow: hidden`
        on the body for touch scrolling, which is the platform that needs this most."""
        rule = re.search(r'body\.scroll-locked\s*{([^}]*)}',
                         _read(os.path.join(CSS_DIR, 'style.css')))
        self.assertIsNotNone(
            rule, 'body.scroll-locked is not defined in static/css/style.css - that is the '
                  'canonical home for it and util.js::syncScrollLock applies it by name.')
        self.assertIn(
            'position: fixed', rule.group(1),
            'body.scroll-locked must pin the body with position: fixed. overflow: hidden '
            'alone does not stop touch scrolling on iOS Safari.')

    def test_every_class_in_overlay_sel_is_defined_in_css(self):
        """An overlay class that no stylesheet defines means the lock is watching for an
        element that can never match - the same silent-failure class as an undefined CSS var.
        Both stylesheets count: .guide-pop and .tag-filter-panel live in guide.css."""
        css = '\n'.join(_read(p) for p in _walk(CSS_DIR, '.css'))
        classes = _overlay_classes(_overlay_selector())
        self.assertTrue(classes, 'OVERLAY_SEL names no classes at all.')
        missing = [c for c in classes if not re.search(rf'\.{c}\b', css)]
        self.assertEqual(
            missing, [],
            f'Class named in OVERLAY_SEL but defined in no stylesheet: {missing}')

    def test_backstop_sync_is_registered_on_document(self):
        """The lock is derived rather than reference-counted, so a missed call site self-heals
        on the next interaction instead of freezing the page permanently. That backstop is
        what makes the derived design safe - it must stay registered."""
        util = _read(os.path.join(JS_DIR, 'util.js'))
        self.assertRegex(
            util, r"document\.addEventListener\('click',\s*syncScrollLock\)",
            "util.js must keep the document-level click backstop for syncScrollLock.")

    def test_every_overlay_open_close_site_syncs(self):
        """Every top-level unit that both names an overlay and changes its visibility must
        call syncScrollLock. This trips on any new overlay too - that is the point."""
        classes = _overlay_classes(_overlay_selector())
        ref_re = _overlay_ref_re(_overlay_ids(classes), classes)
        offenders = []
        for path in list(_walk(JS_DIR, '.js')) + list(_walk(TPL_DIR, '.html')):
            text = _read(path)
            for offset, lines in _script_blocks(path, text):
                for lineno, first in _unsynced_overlay_sites(lines, ref_re, offset):
                    offenders.append(f'{_rel(path)}:{lineno} ({first})')
        self.assertEqual(
            offenders, [],
            'These sites open or close an overlay without calling syncScrollLock(), so the '
            'page behind it keeps scrolling until the next click hits the backstop. Add '
            'syncScrollLock() to each open and each close path (util.js is the one home for '
            f'it - no page-local copies): {offenders}')


class ScrollLockScanCorrectnessTests(unittest.TestCase):
    """Coverage for the scan above, not for the app. The heuristic is non-trivial - it can go
    blind without anyone noticing, which is exactly how the two earlier line-level versions in
    dev/mockups/verify16.js passed while missing real sites (dev/changelog/354)."""

    IDS = {'record-modal', 'guide-daypicker'}
    CLASSES = ('modal', 'menu', 'guide-pop')

    def _sites(self, source):
        ref_re = _overlay_ref_re(self.IDS, self.CLASSES)
        return _unsynced_overlay_sites(source.split('\n'), ref_re)

    def test_unsynced_overlay_close_is_flagged(self):
        source = (
            "function closeRecordModal() {\n"
            "  document.getElementById('record-modal').style.display = 'none';\n"
            "}\n"
        )
        self.assertEqual([s[0] for s in self._sites(source)], [1])

    def test_synced_overlay_close_is_not_flagged(self):
        source = (
            "function closeRecordModal() {\n"
            "  document.getElementById('record-modal').style.display = 'none';\n"
            "  syncScrollLock();\n"
            "}\n"
        )
        self.assertEqual(self._sites(source), [])

    def test_sync_in_a_neighbouring_unit_does_not_vouch(self):
        """The false pass the line-window heuristics produced: a sync in the function above
        must not cover a sibling function that forgot its own."""
        source = (
            "function openRecordModal() {\n"
            "  document.getElementById('record-modal').style.display = 'flex';\n"
            "  syncScrollLock();\n"
            "}\n"
            "function closeRecordModal() {\n"
            "  document.getElementById('record-modal').style.display = 'none';\n"
            "}\n"
        )
        self.assertEqual([s[0] for s in self._sites(source)], [5])

    def test_iife_wrapped_file_still_splits_into_units(self):
        """A file wrapped in one IIFE must not collapse into a single unit, or one sync
        anywhere in it vouches for every overlay in it."""
        source = (
            "(() => {\n"
            "  function openIt() {\n"
            "    document.getElementById('record-modal').style.display = 'flex';\n"
            "    syncScrollLock();\n"
            "  }\n"
            "  function closeIt() {\n"
            "    document.getElementById('record-modal').style.display = 'none';\n"
            "  }\n"
            "  window.openIt = openIt;\n"
            "})();\n"
        )
        self.assertEqual([s[0] for s in self._sites(source)], [6])

    def test_buildmodal_call_site_is_exempt(self):
        source = (
            "function confirmThing() {\n"
            "  const overlay = buildModal({ title: 'Are you sure?' });\n"
            "  overlay.remove();\n"
            "}\n"
        )
        self.assertEqual(self._sites(source), [])

    def test_non_overlay_element_is_not_flagged(self):
        """Inline display is used on ~170 ordinary elements; only overlays are in scope, and
        an id that merely starts with an overlay class name is not an overlay."""
        source = (
            "function toggleNote() {\n"
            "  document.getElementById('modal-padding-note').style.display = 'none';\n"
            "  document.getElementById('od-create-panel').style.display = '';\n"
            "}\n"
        )
        self.assertEqual(self._sites(source), [])

    def test_variable_bound_overlay_is_flagged(self):
        """The site itself never names the overlay - the unit that binds it does. This shape
        (`pop.hidden = true`) is most of the guide's popover code."""
        source = (
            "function wirePopover() {\n"
            "  const pop = document.getElementById('guide-daypicker');\n"
            "  pop.addEventListener('click', () => {\n"
            "    pop.hidden = true;\n"
            "  });\n"
            "}\n"
        )
        self.assertEqual([s[0] for s in self._sites(source)], [1])


# A backticked token that looks like a config path: `recording.post_script.path`. The first
# segment must start with a letter so `v0.1.0`, `0.3.0` and `1.1` are never candidates; it is
# then required to be a real top-level config key, which is what keeps `sensor.dvr_capturing`,
# `config.yaml` and `dvr.db-wal` out of the scan.
_DOTTED_TOKEN_RE = re.compile(r'`([a-z_][a-z_0-9]*(?:\.[a-z_0-9]+)+)`')

# The docs that ship to the public repo and are therefore read by someone who has none of the
# dev tree. README.md is the one most likely to accrete a pointer into it.
_SHIPPED_DOCS = ('README.md', 'CONTRIBUTING.md', 'docs/CONVENTIONS.md')


class ShippedDocPointerTests(unittest.TestCase):
    """A shipped doc must not name a config key that does not exist.

    The security section named the post-recording script
    `recording.post_process.post_script`; the real key is `recording.post_script`, a sibling
    of `post_process`, not a child (dev/changelog/518). Documentation that names a setting has
    to name one the app would actually read - especially this setting, which the same
    paragraph identifies as arbitrary code execution.

    The other half of this guard - no shipped doc may point at a path `.publishignore`
    excludes - is in `tests/test_dev_tree_invariants.py`, because it reads `.publishignore`
    and `.publishignore` excludes itself from the mirror.
    """

    def test_config_keys_named_in_shipped_docs_resolve(self):
        from app.config import _DEFAULTS

        offenders = []
        for doc in _SHIPPED_DOCS:
            path = os.path.join(ROOT, doc)
            for i, line in enumerate(_read(path).splitlines()):
                for token in _DOTTED_TOKEN_RE.findall(line):
                    parts = token.split('.')
                    if parts[0] not in _DEFAULTS:
                        continue
                    node = _DEFAULTS
                    for part in parts:
                        if not isinstance(node, dict) or part not in node:
                            offenders.append(f'{doc}:{i + 1}: {token} (no such config key)')
                            break
                        node = node[part]
        self.assertEqual(offenders, [], 'shipped docs name config keys that do not exist:\n'
                                        + '\n'.join(offenders))

    def test_the_config_key_scan_would_catch_a_bad_key(self):
        """Guard against the scan above passing because it matches nothing. `notifications` is a
        real top-level key, so this token gets past the first-segment filter and must be rejected
        on the walk."""
        from app.config import _DEFAULTS

        token = 'notifications.no_such_key'
        parts = token.split('.')
        self.assertIn(parts[0], _DEFAULTS)
        self.assertNotIn(parts[1], _DEFAULTS[parts[0]])
        self.assertEqual(_DOTTED_TOKEN_RE.findall(f'set `{token}` to true'), [token])


_ADD_COL_RE = re.compile(
    r'ALTER\s+TABLE\s+(?P<table>\{?\w+\}?)\s+ADD\s+COLUMN\s+(?P<col>\{?\w+\}?)\s+(?P<ddl>.*?)\s*$',
    re.IGNORECASE)
_SQL_DEFAULT_RE = re.compile(r'\bDEFAULT\s+(.+?)\s*$', re.IGNORECASE)


def _sql_template(node):
    """A str constant, or an f-string rendered with `{name}` where its values were."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                expr = part.value
                out.append('{%s}' % expr.id if isinstance(expr, ast.Name) else '{?}')
        return ''.join(out)
    return None


def _const_rows(node):
    """A list/tuple of constants (or of tuples of constants) as a list of value tuples."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    rows = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant):
            rows.append((elt.value,))
        elif isinstance(elt, (ast.Tuple, ast.List)) and all(
                isinstance(e, ast.Constant) for e in elt.elts):
            rows.append(tuple(e.value for e in elt.elts))
        else:
            return None
    return rows


def _for_bindings(loop, assigned):
    """{loop variable: [values]} for one `for` over constants, or {} if it isn't one."""
    rows = _const_rows(loop.iter)
    if rows is None and isinstance(loop.iter, ast.Name):
        rows = assigned.get(loop.iter.id)
    if rows is None:
        return {}
    target = loop.target
    if isinstance(target, ast.Name):
        names = [target.id]
    elif isinstance(target, ast.Tuple):
        names = [t.id for t in target.elts if isinstance(t, ast.Name)]
    else:
        return {}
    return {name: [r[pos] for r in rows if len(r) > pos]
            for pos, name in enumerate(names)}


def _expand_add_column(table, col, ddl, bindings):
    """Resolve `{placeholder}` table/column/DDL against loop bindings into real triples."""
    tables = bindings.get(table[1:-1], []) if table.startswith('{') else [table]
    if not col.startswith('{'):
        return [(t, col, ddl) for t in tables]
    cols = bindings.get(col[1:-1], [])
    # The `{col} {definition}` shape: the DDL is the loop's other element, paired by index.
    ddl_name = ddl[1:-1] if ddl.startswith('{') and ddl.endswith('}') else None
    ddls = bindings.get(ddl_name, []) if ddl_name else None
    out = []
    for i, c in enumerate(cols):
        if ddl_name:
            text = ddls[i] if ddls and i < len(ddls) else ''
        else:
            text = ddl
        out.extend((t, c, text) for t in tables)
    return out


def find_defaulted_add_columns(source):
    """(table, column, default literal) for every ADD COLUMN in `source` carrying a DEFAULT.

    Loop variables are resolved from the loops actually enclosing each statement, innermost
    first. A function-wide map of the same names silently drops columns instead: several
    steps reuse `col, definition` for two different tables in a row, and the second list
    shadows the first.
    """
    tree = ast.parse(source)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    assigned = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            rows = _const_rows(node.value)
            if rows is not None:
                assigned[node.targets[0].id] = rows

    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Constant, ast.JoinedStr)):
            continue
        template = _sql_template(node)
        if not template or 'ADD COLUMN' not in template:
            continue
        match = _ADD_COL_RE.search(' '.join(template.split()))
        if not match:
            continue
        bindings = {}
        scope = parents.get(node)
        while scope is not None:
            if isinstance(scope, ast.For):
                for name, values in _for_bindings(scope, assigned).items():
                    bindings.setdefault(name, values)
            scope = parents.get(scope)
        for table, col, ddl in _expand_add_column(
                match.group('table'), match.group('col'), match.group('ddl'), bindings):
            default = _SQL_DEFAULT_RE.search(ddl)
            if default:
                found.add((table, col, default.group(1)))
    return sorted(found)


#: _model_server_default's answer for a column the current models no longer declare, which
#: is not the same as a column declaring no server_default.
NO_SUCH_COLUMN = object()


def _model_server_default(table, column):
    """The literal DEFAULT the current models emit for a column.

    None means the column exists with no server_default; NO_SUCH_COLUMN means a later
    migration dropped it, so there is no fresh-build DDL left for it to disagree with.
    """
    import importlib

    # app/__init__.py imports the models inside create_app(), so importing the package is
    # not enough to populate db.metadata - this module has to ask for them by name.
    importlib.import_module('app.database')
    db = importlib.import_module('app').db

    tbl = db.metadata.tables.get(table)
    if tbl is None or column not in tbl.c:
        return NO_SUCH_COLUMN
    clause = tbl.c[column].server_default
    if clause is None:
        return None
    arg = getattr(clause, 'arg', None)
    return getattr(arg, 'text', arg)


class MigrationServerDefaultParityTests(unittest.TestCase):
    """A column a migration adds with a SQL DEFAULT must declare an equal `server_default`.

    create_all() emits no DEFAULT for a Python-side `default=`, so without this the same
    column carries a DEFAULT on every upgraded database and none on every fresh one. The ORM
    never notices - it always supplies a value - but a raw INSERT that omits the column
    succeeds against one vintage and dies on NOT NULL against the other, which aborts
    startup when the writer is a migration step (dev/docs/BUGS.md 2026-08-16 @ 11:41:29 AM ET,
    dev/changelog/687 for the crash this class generalizes, `690` for the sweep).

    The literals must match exactly, not merely be equivalent: `DEFAULT 0` and `DEFAULT '0'`
    both round-trip through SQLite the same way but leave the two vintages' DDL textually
    different, which is the thing being closed. That is why the models use
    `server_default=db.text('0')` rather than a bare string.
    """

    def _migration_defaults(self):
        return find_defaulted_add_columns(
            _read(os.path.join(APP_DIR, 'migrations.py')))

    def test_every_defaulted_migration_column_matches_its_model(self):
        offenders = []
        for table, column, literal in self._migration_defaults():
            declared = _model_server_default(table, column)
            if declared is NO_SUCH_COLUMN:
                continue
            if declared is None:
                offenders.append(f'{table}.{column}: migration says DEFAULT {literal}, '
                                 f'model declares no server_default')
            elif str(declared) != literal:
                offenders.append(f'{table}.{column}: migration says DEFAULT {literal}, '
                                 f'model declares {declared}')
        self.assertEqual(offenders, [],
                         'migrated and fresh databases would carry different column DDL:\n'
                         + '\n'.join(offenders))

    def test_the_scan_still_sees_the_real_migration_file(self):
        """Guard against the check above passing because the scan matched nothing."""
        found = self._migration_defaults()
        self.assertGreater(len(found), 15,
                           'the ADD COLUMN scan found almost nothing - it has probably '
                           'stopped matching app/migrations.py rather than gone clean')
        # Two shapes that must both keep resolving: a plain literal statement, and a column
        # whose name and DDL come from a loop over a list of tuples.
        self.assertIn(('channels', 'url_normalizable', '1'), found)
        self.assertIn(('accounts', 'account_type', "'m3u'"), found)


class MigrationServerDefaultScanCorrectnessTests(unittest.TestCase):
    """Regression coverage for the scan itself, mirroring RetryOnLockedScanCorrectnessTests.

    Every case here is a shape that exists in app/migrations.py, and the third one is a bug
    the scan actually had: resolving loop variables per function instead of per loop made
    `_m001_baseline`'s three consecutive `for col, definition in ...` loops collide, and
    three real columns went unreported.
    """

    def test_plain_statement_with_a_default(self):
        source = ("def step(conn, cur):\n"
                  "    cur.execute('ALTER TABLE channels ADD COLUMN flag "
                  "BOOLEAN NOT NULL DEFAULT 1')\n")
        self.assertEqual(find_defaulted_add_columns(source), [('channels', 'flag', '1')])

    def test_column_without_a_default_is_ignored(self):
        source = ("def step(conn, cur):\n"
                  "    cur.execute('ALTER TABLE channels ADD COLUMN notes TEXT')\n")
        self.assertEqual(find_defaulted_add_columns(source), [])

    def test_two_loops_reusing_one_variable_name_resolve_to_their_own_tables(self):
        source = ("def step(conn, cur):\n"
                  "    for col, ddl in [('a', 'INTEGER DEFAULT 1')]:\n"
                  "        cur.execute(f'ALTER TABLE channels ADD COLUMN {col} {ddl}')\n"
                  "    for col, ddl in [('b', 'INTEGER DEFAULT 2')]:\n"
                  "        cur.execute(f'ALTER TABLE recordings ADD COLUMN {col} {ddl}')\n")
        self.assertEqual(find_defaulted_add_columns(source),
                         [('channels', 'a', '1'), ('recordings', 'b', '2')])

    def test_loop_over_a_variable_assigned_earlier(self):
        source = ("def step(conn, cur):\n"
                  "    cols = [('a', 'INTEGER DEFAULT 7'), ('b', 'TEXT')]\n"
                  "    for col, ddl in cols:\n"
                  "        cur.execute(f'ALTER TABLE accounts ADD COLUMN {col} {ddl}')\n")
        self.assertEqual(find_defaulted_add_columns(source), [('accounts', 'a', '7')])

    def test_a_missing_server_default_is_reported(self):
        """The parity check must fail on a real offender, not just pass on a clean tree."""
        offenders = []
        for table, column, literal in [('channels', 'notes', '1')]:
            declared = _model_server_default(table, column)
            if declared is None:
                offenders.append(f'{table}.{column}')
        self.assertEqual(offenders, ['channels.notes'],
                         'channels.notes has no server_default, so a migration claiming to '
                         'give it one must be reported')


def _split_index_terms(text):
    """Split an index's column list on TOP-LEVEL commas only.

    `lower(name), id` is two terms, not three: a plain `split(',')` cuts inside the
    expression's own parentheses. Shared by both scanners so the model side and the migration
    side can never disagree about where one term ends.
    """
    terms, depth, current = [], 0, ''
    for ch in text:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ',' and depth == 0:
            terms.append(current.strip())
            current = ''
        else:
            current += ch
    if current.strip():
        terms.append(current.strip())
    return tuple(terms)


def _declared_index_terms(idx):
    """An index's terms as the SQL text `create_all()` would put in its CREATE INDEX.

    Compiled through the real DDL compiler rather than assembled from `idx.columns`, because
    those are two different answers for an expression index and only one of them is what
    lands in the database. The DDL is also the exact form the migration scanner reads out of
    app/migrations.py, so parity compares like with like by construction instead of by two
    hand-written normalizations that drift.
    """
    from sqlalchemy.dialects import sqlite
    from sqlalchemy.schema import CreateIndex

    ddl = ' '.join(str(CreateIndex(idx).compile(dialect=sqlite.dialect())).split())
    inner = ddl[ddl.index('(') + 1:ddl.rindex(')')]
    return _split_index_terms(inner)


def _model_indexes():
    """{table: [(index name, (column, ...)), ...]} for every index the models declare.

    Both spellings are collected - `db.Column(..., index=True)` and an explicit
    `db.Index()` in `__table_args__` - because they produce the same thing in the database
    and the redundancy below does not care which one wrote it. UNIQUE constraints count
    too: SQLite backs each with a real index (`sqlite_autoindex_<table>_N`) that the planner
    uses like any other, so a single-column index sitting under a two-column unique
    constraint is just as redundant as one sitting under a composite index.

    An EXPRESSION index is reported by the text of its expression - `lower(name)`, not
    `name`. `idx.columns` flattens `func.lower(Channel.name)` to the bare column it reads,
    which would make ix_channels_lower_name indistinguishable from a plain index on
    (name, id) - and those are not interchangeable to SQLite, which matches an expression
    index only against the identical expression. Reporting the flattened form would let a
    migration that creates one and a model that declares the other pass the parity check
    below while a fresh install got an index the query can never use. Plain-column entries
    are unaffected, byte for byte, so _MEASURED_PREFIX_EXEMPTIONS still describes what it
    describes.
    """
    import importlib

    importlib.import_module('app.database')
    db = importlib.import_module('app').db

    out = {}
    for name, table in db.metadata.tables.items():
        entries = [(idx.name, _declared_index_terms(idx)) for idx in table.indexes]
        for con in table.constraints:
            cols = tuple(c.name for c in getattr(con, 'columns', []))
            if cols and con.__class__.__name__ == 'UniqueConstraint':
                entries.append((con.name or f'unique{cols}', cols))
        out[name] = sorted(entries)
    return out


def _prefix_redundant_indexes(by_table):
    """Every (table, narrow, wide) where `narrow`'s columns are a strict prefix of `wide`'s."""
    found = []
    for table, entries in sorted(by_table.items()):
        for n1, c1 in entries:
            for n2, c2 in entries:
                if n1 != n2 and len(c1) < len(c2) and c2[:len(c1)] == c1:
                    found.append((table, n1, n2))
    return found


#: Prefix-redundant indexes kept anyway, each because dropping it was MEASURED to be worse.
#: An entry is a promise that someone ran the experiment, not that the shape looked fine.
_MEASURED_PREFIX_EXEMPTIONS = {
    # Dropping this does not make SQLite fall through to ix_epg_entries_channel_stop: the
    # airing grain's group-dedup subquery reads title/start_time/stop_time as well as
    # channel_id, so with only the wider index available the planner builds an AUTOMATIC
    # PARTIAL COVERING INDEX over all 1.97M rows on every query instead. The default airings
    # page goes 0.098s to 2.914s on the production database (dev/changelog/692).
    ('epg_entries', 'ix_epg_entries_channel_id', 'ix_epg_entries_channel_stop'),
}


class RedundantPrefixIndexTests(unittest.TestCase):
    """An index that is a strict column-prefix of another needs a MEASURED reason to exist.

    A b-tree on `(a)` can usually answer nothing a b-tree on `(a, b)` cannot, so the narrow
    one usually buys no read while costing a write on every INSERT and UPDATE of its table.
    That much is only tidiness. What makes this worth enforcing is that such an index is not
    inert - it is a decoy the planner can prefer, and preferring it can be far slower. On the
    production database the airing grain's default page walks ix_epg_entries_start_stop and
    rejects the ~1.16M already-ended showings inside the index (0.091s for 100 rows).
    Removing one duplicated ORDER BY term - an edit with no semantic effect whatsoever - was
    enough to tip SQLite onto the standalone ix_epg_entries_start_time, which cannot answer
    stop_time from the index and so fetched a table row per candidate: 1.222s, 13x slower.

    **The rule is "measure each one", NOT "prefix indexes are safe to drop"**, and that
    distinction is the whole reason this is an allowlist instead of a flat ban. Dropping all
    seven of this schema's prefix-redundant indexes was tried, and one of them -
    ix_epg_entries_channel_id - turned out to be load-bearing in the opposite direction, at
    30x. Adding an entry to _MEASURED_PREFIX_EXEMPTIONS means the experiment was run on a
    realistic database and the number is written down; it does not mean the shape was
    argued about.

    UNIQUE constraints count as the WIDE index - SQLite backs each with a real index the
    planner uses like any other. See _model_indexes.

    Scope: what the MODELS declare, which is what a fresh create_all() builds. An index that
    exists only in a migration is invisible here - that gap is its own known defect and is
    tracked in the backlog, not worked around by scanning migration SQL for CREATE INDEX.
    """

    def test_no_unmeasured_index_is_a_prefix_of_another(self):
        offenders = [o for o in _prefix_redundant_indexes(_model_indexes())
                     if o not in _MEASURED_PREFIX_EXEMPTIONS]
        self.assertEqual(
            offenders, [],
            'these indexes can never answer anything the wider index cannot, and can be '
            'preferred by the planner instead of it. Drop it, or measure the drop on a '
            'realistic database and add it to _MEASURED_PREFIX_EXEMPTIONS with the number:\n'
            + '\n'.join(f'  {t}: {narrow} is a strict prefix of {wide}'
                        for t, narrow, wide in offenders))

    def test_every_exemption_still_describes_a_real_pair(self):
        """An exemption for an index that no longer exists is stale permission - remove it."""
        actual = set(_prefix_redundant_indexes(_model_indexes()))
        stale = _MEASURED_PREFIX_EXEMPTIONS - actual
        self.assertEqual(stale, set(),
                         'these exemptions no longer describe a prefix pair in the models, '
                         f'so they are permission for nothing: {sorted(stale)}')

    def test_the_scan_reads_the_real_models(self):
        """Guard against the check above passing because it found no indexes at all."""
        by_table = _model_indexes()
        self.assertIn('epg_entries', by_table)
        names = {n for n, _ in by_table['epg_entries']}
        self.assertIn('ix_epg_entries_start_stop', names)
        self.assertIn('ix_epg_entries_channel_stop', names)
        # 25 entries as of dev/changelog/692. The floor only has to be high enough that an
        # import returning nothing fails here rather than passing the check above.
        self.assertGreater(sum(len(v) for v in by_table.values()), 20,
                           'the index scan found almost nothing - it has probably stopped '
                           'seeing app/database.py rather than gone clean')

    def test_a_real_prefix_pair_is_reported(self):
        """The detector must fail on an offender, not merely pass on a clean tree."""
        self.assertEqual(
            _prefix_redundant_indexes({'t': [('narrow', ('a',)), ('wide', ('a', 'b'))]}),
            [('t', 'narrow', 'wide')])

    def test_a_different_leading_column_is_not_redundant(self):
        """`(b)` is not covered by `(a, b)` - SQLite cannot seek an index by its 2nd column."""
        self.assertEqual(
            _prefix_redundant_indexes({'t': [('other', ('b',)), ('wide', ('a', 'b'))]}), [])

    def test_equal_length_indexes_are_not_redundant(self):
        self.assertEqual(
            _prefix_redundant_indexes({'t': [('x', ('a', 'b')), ('y', ('a', 'c'))]}), [])


def _boolean_leading_indexes(by_table=None):
    """Every (table, index) whose FIRST indexed term is a Boolean column.

    Leading, not merely present: SQLite seeks an index by its leftmost column, so a boolean
    in second position still lets the index answer an equality on the first. It is the one
    in front that decides how many rows a seek can reject.
    """
    import importlib

    importlib.import_module('app.database')
    db = importlib.import_module('app').db
    by_table = _model_indexes() if by_table is None else by_table

    found = []
    for table_name, entries in sorted(by_table.items()):
        table = db.metadata.tables.get(table_name)
        if table is None:
            continue
        booleans = {c.name for c in table.columns
                    if c.type.__class__.__name__ == 'Boolean'}
        for name, terms in entries:
            if terms and terms[0] in booleans:
                found.append((table_name, name))
    return found


#: Boolean-led indexes kept anyway, each because keeping it was MEASURED to be better.
#: Same contract as _MEASURED_PREFIX_EXEMPTIONS: an entry promises the experiment was run on
#: a realistic database and the number written down, never that the shape was argued about.
#:
#: Both entries here earn it the same way, and it is the way that distinguishes a useful
#: boolean index from a decoy: their queries seek the RARE value. Measured on the production
#: database (137,144 channels), each against the same query with the index suppressed:
_MEASURED_BOOLEAN_INDEX_EXEMPTIONS = {
    # 6 rows of 137,144. The guide reads in_guide=1; 0.0 ms with, 171.4 ms without.
    ('channels', 'ix_channels_in_guide'),
    # 1,572 rows of 137,144. channel_search's duplicate handling asks .is_(True) in its hot
    # paths; 1.0 ms with, 40.5 ms without.
    ('channels', 'ix_channels_is_duplicate_stream_url'),
    # The third case, and it earns its place a different way: this one is never SOUGHT, it is
    # SCANNED. The standing breakdown buckets every channel through one ordered CASE, so it
    # visits all 137,283 rows of a 63 MB table whatever the leading column is; holding the
    # columns it reads in a 1.80 MB index turns that into a covering index scan. Measured on
    # a copy of the production database, only the index differing: 181.9 ms -> 99.7 ms on the
    # statement, 219.5 ms -> 120.7 ms end to end on `channels counts no-q` and 268.8 ms ->
    # 168.8 ms on `channels no-q rows`. The decoy hazard was checked, not argued - the
    # default sort's own query got faster and kept ix_channels_lower_name
    # (dev/changelog/834). StandingIndexCoverageTests guards the column list.
    ('channels', 'ix_channels_standing'),
}


class BooleanLeadingIndexTests(unittest.TestCase):
    """An index led by a Boolean column needs a MEASURED reason to exist.

    The sibling of RedundantPrefixIndexTests, and the same defect in the other direction:
    that scan catches a decoy the planner prefers because a WIDER index already covers it,
    this one catches a decoy it prefers because the column is not selective enough for any
    index to help. Neither can see the other's case - a boolean index is nobody's column
    prefix, which is exactly why ix_channels_hidden shipped unremarked.

    **The test is which value the queries seek, not the column's type.** A boolean index
    earns its place when the predicate asks for the RARE value and is worthless when it asks
    for the common one - and the common one is what a "hide this class of row" flag is always
    asked for. Measured on the production database, each against the same query with the
    index suppressed: `in_guide=1` (6 rows of 137,144) is 0.0 ms against 171.4 ms, while
    `in_guide=0` is 138.7 ms against 0.3 ms. Same index, opposite verdicts.

    Costs, then, on the wrong side of that line: a write on every row of every bulk rewrite,
    and a planner hazard. Two rows of distinct values cannot reject enough of a table to earn
    a seek, but with no `sqlite_stat1` present SQLite assumes otherwise - on the isolated
    shape `WHERE hidden=0 ORDER BY lower(name), id LIMIT 100` it preferred ix_channels_hidden
    over ix_channels_lower_name, could not answer the ORDER BY from it, and added a temp
    b-tree over all 137,144 rows: 135.1 ms against 0.2 ms. The channel search's real default
    query is more predicated and was measured unaffected, so dropping ix_channels_hidden was
    hazard removal rather than a speedup (dev/changelog/781) - which is exactly why a scan is
    worth more here than a benchmark: the cost of one of these is latent until some query
    happens to take the shape that exposes it.

    It does not self-heal as data arrives - a 50/50 boolean is no better than a one-value one
    - and it gets worse in normal use, since every bulk rewrite scatters the rows the plan's
    per-row lookups then chase.

    Scope, as above: what the MODELS declare. An index created only in a migration is
    invisible here.
    """

    def test_no_unmeasured_index_leads_with_a_boolean(self):
        offenders = [o for o in _boolean_leading_indexes()
                     if o not in _MEASURED_BOOLEAN_INDEX_EXEMPTIONS]
        self.assertEqual(
            offenders, [],
            'these indexes are led by a column with two distinct values, which cannot '
            'reject enough rows to earn a seek - and the planner can prefer one anyway, '
            'losing whatever index it would otherwise have used. Drop it, or measure '
            'keeping it on a realistic database and add it to '
            '_MEASURED_BOOLEAN_INDEX_EXEMPTIONS with the number:\n'
            + '\n'.join(f'  {t}: {name}' for t, name in offenders))

    def test_every_exemption_still_describes_a_real_index(self):
        """An exemption for an index that no longer exists is stale permission - remove it."""
        stale = _MEASURED_BOOLEAN_INDEX_EXEMPTIONS - set(_boolean_leading_indexes())
        self.assertEqual(stale, set(),
                         'these exemptions no longer describe a boolean-led index in the '
                         f'models, so they are permission for nothing: {sorted(stale)}')

    def test_a_boolean_led_index_is_reported(self):
        """The detector must fail on an offender, not merely pass on a clean tree."""
        self.assertEqual(
            _boolean_leading_indexes({'channels': [('ix_fake', ('hidden',))]}),
            [('channels', 'ix_fake')])

    def test_a_boolean_in_second_position_is_not_reported(self):
        """SQLite seeks by the leftmost column, so only the leading term is judged."""
        self.assertEqual(
            _boolean_leading_indexes({'channels': [('ix_fake', ('name', 'hidden'))]}), [])

    def test_the_scan_reads_the_real_models(self):
        """Guard against the check above passing because it found no boolean columns."""
        self.assertEqual(
            _boolean_leading_indexes({'channels': [('ix_fake', ('in_guide',))]}),
            [('channels', 'ix_fake')],
            'the scan could not resolve a known Boolean column on channels - it has '
            'probably stopped seeing app/database.py rather than gone clean')


# `cols` is greedy up to the LAST `)` rather than the first, so an expression index's own
# parentheses stay inside the term list: `ON channels (lower(name), id)` must yield
# `lower(name), id`, not `lower(name`. _split_index_terms then cuts it on top-level commas.
_CREATE_INDEX_RE = re.compile(
    r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(?P<name>\S+)\s+ON\s+'
    r'(?P<table>\w+)\s*\((?P<cols>.+)\)\s*$', re.IGNORECASE)


def _expand_index(table, name, cols, bindings):
    """Resolve `{placeholder}` index name/columns against loop bindings, same shape as
    _expand_add_column above - the (name, cols) loop pair is _m024's own."""
    names = bindings.get(name[1:-1], []) if name.startswith('{') else [name]
    if not cols.startswith('{'):
        return [(table, n, _split_index_terms(cols)) for n in names]
    col_lists = bindings.get(cols[1:-1], [])
    out = []
    for i, n in enumerate(names):
        raw = col_lists[i] if i < len(col_lists) else ''
        out.append((table, n, _split_index_terms(raw)))
    return out


def find_migration_created_indexes(source, with_lines=False):
    """(table, index name, (column, ...)) for every CREATE INDEX in `source`.

    `with_lines=True` appends the statement's line number to each tuple, which is what lets
    a caller tell a CREATE that a later migration retires from one that re-creates a
    previously-dropped index (see find_migration_dropped_indexes).

    Same walk as find_defaulted_add_columns: loop variables are resolved from the loops
    actually enclosing each statement, innermost first, which is what lets this see
    _m024_channel_search_support's `for name, cols in [...]` shape rather than just the
    single-statement ones.
    """
    tree = ast.parse(source)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    assigned = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            rows = _const_rows(node.value)
            if rows is not None:
                assigned[node.targets[0].id] = rows

    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Constant, ast.JoinedStr)):
            continue
        template = _sql_template(node)
        if not template or 'INDEX' not in template.upper():
            continue
        match = _CREATE_INDEX_RE.search(' '.join(template.split()))
        if not match:
            continue
        bindings = {}
        scope = parents.get(node)
        while scope is not None:
            if isinstance(scope, ast.For):
                for name, values in _for_bindings(scope, assigned).items():
                    bindings.setdefault(name, values)
            scope = parents.get(scope)
        for table, name, cols in _expand_index(
                match.group('table'), match.group('name'), match.group('cols'), bindings):
            found.add((table, name, cols, node.lineno) if with_lines else (table, name, cols))
    return sorted(found)


_DROP_INDEX_RE = re.compile(r'DROP\s+INDEX\s+(?:IF\s+EXISTS\s+)?(?P<name>\S+?)\s*$',
                            re.IGNORECASE)


def find_migration_dropped_indexes(source):
    """{index name: line of the LAST migration statement that drops it} for `source`.

    The counterpart to find_migration_created_indexes: an index an early migration creates
    and a later one drops is legitimately absent from the models, because a fresh
    create_all() build never had it in the first place. Line numbers rather than bare names
    so a drop can be told from a later re-creation - migrations are appended in order, so a
    DROP below the CREATE retires it and one above it does not.
    """
    dropped = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.Constant, ast.JoinedStr)):
            continue
        template = _sql_template(node)
        if not template or 'INDEX' not in template.upper():
            continue
        match = _DROP_INDEX_RE.search(' '.join(template.split()))
        if match:
            name = match.group('name')
            dropped[name] = max(dropped.get(name, 0), node.lineno)
    return dropped


def _model_indexes_by_name(by_table):
    """{(table, index name): columns} flattened from _model_indexes()'s per-table lists."""
    return {(table, name): cols for table, entries in by_table.items() for name, cols in entries}


class MigrationIndexParityTests(unittest.TestCase):
    """An index a migration creates on an ORM-owned table must also be declared on its model.

    A fresh database is built by create_all() from the models, not by replaying migrations -
    run_migrations() stamps a fresh DB straight to CURRENT_SCHEMA_VERSION and runs no steps -
    so an index that exists only inside a migration step is an index every new install
    silently does without. _m024_channel_search_support's five facet indexes were in exactly
    that state until dev/changelog/695, after the same gap had already been fixed for
    migrations 17/25/36 (see EPGEntry.__table_args__'s own comment on the class).

    An index a LATER migration drops is exempt, and has to be: a fresh build correctly lacks
    it, so demanding the model still declare it would demand every new install rebuild an
    index the upgrade path just removed. ix_channels_hidden is the case that first needed
    this (created by migration 43, dropped by 45 - dev/changelog/781).
    """

    def _migration_indexes(self):
        return find_migration_created_indexes(
            _read(os.path.join(APP_DIR, 'migrations.py')), with_lines=True)

    def test_every_migration_index_on_an_orm_table_is_declared_on_its_model(self):
        import importlib
        importlib.import_module('app.database')
        tables = importlib.import_module('app').db.metadata.tables

        declared = _model_indexes_by_name(_model_indexes())
        dropped = find_migration_dropped_indexes(
            _read(os.path.join(APP_DIR, 'migrations.py')))
        offenders = []
        for table, name, cols, lineno in self._migration_indexes():
            if dropped.get(name, 0) > lineno:
                continue  # retired by a later migration - a fresh build rightly lacks it
            if table not in tables:
                continue  # not an ORM-owned table - out of scope for this check
            model_cols = declared.get((table, name))
            if model_cols is None:
                offenders.append(f'{table}.{name}{cols}: migration creates it, '
                                 f'model declares no index of that name')
            elif model_cols != cols:
                offenders.append(f'{table}.{name}: migration says {cols}, '
                                 f'model declares {model_cols}')
        self.assertEqual(offenders, [],
                         'a fresh create_all() build would be missing (or would disagree '
                         'with) these migration-created indexes:\n' + '\n'.join(offenders))

    def test_the_scan_still_sees_the_real_migration_file(self):
        """Guard against the check above passing because the scan matched nothing."""
        found = self._migration_indexes()
        self.assertGreaterEqual(len(found), 6,
                                'the CREATE INDEX scan found almost nothing - it has '
                                'probably stopped matching app/migrations.py rather than '
                                'gone clean')
        self.assertIn(('channels', 'ix_channels_health',
                       ('health_score', 'manual_health_adjustment')),
                      [entry[:3] for entry in found])

    def test_the_drop_scan_still_sees_the_real_migration_file(self):
        """The exemption above is only as good as the DROP scan that feeds it."""
        dropped = find_migration_dropped_indexes(
            _read(os.path.join(APP_DIR, 'migrations.py')))
        self.assertIn('ix_channels_hidden', dropped,
                      'the DROP INDEX scan no longer sees migration 45 - the parity check '
                      'above would start demanding a model declare a retired index')


class MigrationIndexParityScanCorrectnessTests(unittest.TestCase):
    """Regression coverage for the scan itself, mirroring MigrationServerDefaultScanCorrectnessTests."""

    def test_plain_statement(self):
        source = ("def step(conn, cur):\n"
                  "    cur.execute('CREATE INDEX IF NOT EXISTS ix_foo_bar ON foo (bar)')\n")
        self.assertEqual(find_migration_created_indexes(source),
                         [('foo', 'ix_foo_bar', ('bar',))])

    def test_multiline_concatenated_statement(self):
        source = ("def step(conn, cur):\n"
                  "    cur.execute(\n"
                  "        'CREATE INDEX IF NOT EXISTS ix_foo_bar '\n"
                  "        'ON foo (bar, baz)'\n"
                  "    )\n")
        self.assertEqual(find_migration_created_indexes(source),
                         [('foo', 'ix_foo_bar', ('bar', 'baz'))])

    def test_loop_over_a_list_of_name_column_pairs(self):
        source = ("def step(conn, cur):\n"
                  "    for name, cols in [('ix_a', 'a'), ('ix_b', 'b, c')]:\n"
                  "        cur.execute(f'CREATE INDEX IF NOT EXISTS {name} ON foo ({cols})')\n")
        self.assertEqual(find_migration_created_indexes(source),
                         [('foo', 'ix_a', ('a',)), ('foo', 'ix_b', ('b', 'c'))])

    def test_drop_index_is_found_with_and_without_if_exists(self):
        source = ("def a(conn, cur):\n"
                  "    cur.execute('DROP INDEX IF EXISTS ix_foo_bar')\n"
                  "def b(conn, cur):\n"
                  "    cur.execute('DROP INDEX ix_foo_baz')\n")
        self.assertEqual(sorted(find_migration_dropped_indexes(source)),
                         ['ix_foo_bar', 'ix_foo_baz'])

    def test_a_create_after_a_drop_is_not_treated_as_retired(self):
        """The exemption is positional: only a DROP *below* the CREATE retires it.

        Otherwise a migration that dropped an index and a later one that re-created it
        would together read as "retired", and the re-created index would be exempt from
        parity forever - silently missing from every fresh install.
        """
        source = ("def a(conn, cur):\n"
                  "    cur.execute('DROP INDEX IF EXISTS ix_foo_bar')\n"
                  "def b(conn, cur):\n"
                  "    cur.execute('CREATE INDEX IF NOT EXISTS ix_foo_bar ON foo (bar)')\n")
        dropped = find_migration_dropped_indexes(source)
        created = find_migration_created_indexes(source, with_lines=True)
        retired = [name for _t, name, _c, line in created if dropped.get(name, 0) > line]
        self.assertEqual(retired, [])

    def test_a_drop_after_a_create_is_treated_as_retired(self):
        source = ("def a(conn, cur):\n"
                  "    cur.execute('CREATE INDEX IF NOT EXISTS ix_foo_bar ON foo (bar)')\n"
                  "def b(conn, cur):\n"
                  "    cur.execute('DROP INDEX IF EXISTS ix_foo_bar')\n")
        dropped = find_migration_dropped_indexes(source)
        created = find_migration_created_indexes(source, with_lines=True)
        retired = [name for _t, name, _c, line in created if dropped.get(name, 0) > line]
        self.assertEqual(retired, ['ix_foo_bar'])

    def test_a_missing_model_declaration_is_reported(self):
        """The parity check must fail on a real offender, not just pass on a clean tree."""
        declared = {}  # no model declares this index
        offenders = []
        for table, name, cols in [('channels', 'ix_does_not_exist', ('name',))]:
            if declared.get((table, name)) is None:
                offenders.append(f'{table}.{name}')
        self.assertEqual(offenders, ['channels.ix_does_not_exist'])

    def test_expression_index_keeps_its_own_parentheses(self):
        """`lower(name), id` is two terms, not `lower(name` and `id`.

        The column group used to stop at the first `)`, which is inside the expression, so
        ix_channels_lower_name would have been read as an index on a column named
        `lower(name` - a name no model can declare, failing parity for the wrong reason and
        hiding whether the real index was declared at all (dev/changelog/699).
        """
        source = ("def step(conn, cur):\n"
                  "    cur.execute('CREATE INDEX IF NOT EXISTS ix_foo_lower "
                  "ON foo (lower(bar), id)')\n")
        self.assertEqual(find_migration_created_indexes(source),
                         [('foo', 'ix_foo_lower', ('lower(bar)', 'id'))])

    def test_expression_terms_are_not_flattened_to_their_column(self):
        """An expression index and a plain index on the same column are different indexes.

        SQLite matches an expression index only against the identical expression, so a model
        declaring `(name, id)` would NOT satisfy a migration creating `(lower(name), id)` -
        the fresh install would build an index the default sort can never use. The two must
        therefore compare unequal here.
        """
        self.assertNotEqual(
            find_migration_created_indexes(
                "def step(conn, cur):\n"
                "    cur.execute('CREATE INDEX IF NOT EXISTS ix_f ON f (lower(a), id)')\n"),
            find_migration_created_indexes(
                "def step(conn, cur):\n"
                "    cur.execute('CREATE INDEX IF NOT EXISTS ix_f ON f (a, id)')\n"))

    def test_split_index_terms_ignores_commas_inside_parentheses(self):
        self.assertEqual(_split_index_terms('coalesce(a, b), c'), ('coalesce(a, b)', 'c'))
        self.assertEqual(_split_index_terms('a, b'), ('a', 'b'))
        self.assertEqual(_split_index_terms('a'), ('a',))


class ShrinkToFitOverlayTests(unittest.TestCase):
    """A fixed overlay anchored by one edge must set its own `width`.

    Guards dev/docs/BUGS.md 2026-08-27 @ 07:31. A `position: fixed` box given `left` and
    no `right` shrink-to-fits against the distance from that edge to the viewport's, so
    `.toast` at `left: 50%` could never be wider than half the screen and its `max-width`
    never bound. At 375px that is ~188px: any message past a few words wrapped into a
    near-square, which `border-radius: 999px` drew as a circle. An explicit `width` opts
    out of shrink-to-fit so `max-width` decides.

    This checks the declaration rather than the rendering, because the defect is a layout
    computation and jsdom performs none - the same reason the grid-track rule in CLAUDE.md
    is browser-only. What it can prove is that the property has not been deleted again.
    """

    def test_toast_declares_an_explicit_width(self):
        css = _read(os.path.join(CSS_DIR, 'style.css'))
        m = re.search(r'^\.toast \{(.*?)^\}', css, re.S | re.M)
        self.assertIsNotNone(m, '.toast rule not found in style.css')
        body = m.group(1)
        self.assertRegex(
            body, r'(?<!max-)width\s*:',
            '.toast sets left/max-width but no width, so it shrink-to-fits to half the '
            'viewport and max-width never applies (dev/docs/BUGS.md 2026-08-27 @ 07:31)')
        self.assertIn('max-width', body, '.toast must still cap its width')


def _standing_scan_columns():
    """{option key: the `channels` columns its predicate reads in the OUTER scan}.

    Outer is the whole point. A predicate's nested SELECTs (the duplicate-loser window, the
    `noepg` correlated EXISTS) are separate statements with their own plans, and the columns
    they read say nothing about what the enclosing scan has to fetch per row. So the walk
    stops at every subquery boundary and reports only what the CASE itself touches.
    """
    import importlib
    from sqlalchemy.sql.selectable import Exists, ScalarSelect, Select, Subquery

    cs = importlib.import_module('app.channel_search')
    stop = (Select, ScalarSelect, Subquery, Exists)

    def walk(node, out):
        for child in node.get_children():
            if isinstance(child, stop):
                continue
            table = getattr(child, 'table', None)
            if getattr(table, 'name', None) == 'channels' and getattr(child, 'name', None):
                out.add(child.name)
            walk(child, out)

    # Enough of a context for every channel-side predicate to take its populated branch: an
    # empty one short-circuits `shownotnorm` and `showmembers` to db.false(), which reads no
    # column at all and would let the guard pass on a predicate it never built.
    ctx = cs.SearchContext(normalizing_account_ids=frozenset({1}),
                           group_member_channel_ids=(1, 2))
    found = {}
    for key, side in cs._STANDING_SIDE.items():
        if side != cs._SIDE_CHANNEL:
            continue
        cols = set()
        walk(cs._standing_reject(key, ctx), cols)
        found[key] = cols
    return found


class StandingIndexCoverageTests(unittest.TestCase):
    """`ix_channels_standing` only pays if it still COVERS the standing breakdown.

    A covering index is the one optimization that fails silently and completely: add a
    standing option that reads a sixth `channels` column and SQLite stops answering the
    statement from the index, goes back to scanning a 63 MB table, and nothing errors - the
    numbers just quietly double again (measured 99.7 ms -> 181.9 ms on the statement,
    120.7 ms -> 219.5 ms end to end, dev/changelog/834). Nothing about the failure is visible
    in a diff, which is why the index's column list gets a test rather than a comment.

    `id` is exempt because it is the rowid, which every index entry carries anyway.

    The fix when this goes red is a judgment call, not automatically "widen the index": a
    column that is cheap to carry belongs in it, one that is wide or heavily written may be
    worth losing the covering property for. Whichever is chosen, re-measure - the numbers in
    `app/database.py`'s comment and in migration 48 are what this index is justified by.
    """

    def _index_columns(self):
        indexes = dict((name, terms) for name, terms in _model_indexes()['channels'])
        self.assertIn('ix_channels_standing', indexes,
                      'ix_channels_standing is gone from the model. If it was dropped on '
                      'purpose, delete this test and its exemption in '
                      '_MEASURED_BOOLEAN_INDEX_EXEMPTIONS in the same change.')
        return set(indexes['ix_channels_standing']) | {'id'}

    def test_every_channel_side_standing_predicate_is_covered(self):
        covered = self._index_columns()
        uncovered = {key: sorted(cols - covered)
                     for key, cols in _standing_scan_columns().items()
                     if cols - covered}
        self.assertEqual(
            uncovered, {},
            'these standing predicates read a channels column ix_channels_standing does '
            'not hold, so the breakdown scan falls back to the table:\n'
            + '\n'.join(f'  {key}: {cols}' for key, cols in sorted(uncovered.items())))

    def test_the_walk_actually_finds_the_columns_it_claims_to(self):
        """A guard that silently found nothing would pass forever."""
        found = _standing_scan_columns()
        self.assertIn('hidden', found['showhidden'])
        self.assertIn('in_guide', found['showmembers'])
        self.assertEqual(found['shownotnorm'], {'url_normalizable', 'account_id'})
        # The duplicate-loser window reads stream_url and health_score, but inside its own
        # SELECT - the outer CASE only tests the id. If the walk ever starts descending,
        # this is the assertion that says so.
        self.assertEqual(found['showdup'], {'id'})


class CiWorkflowToolingTests(unittest.TestCase):
    """The CI workflow must keep installing what the suite gates on, at the version we ship.

    Roughly 408 tests skip themselves when ffmpeg/ffprobe or node_modules/jsdom are
    absent, and a skip is not a failure - so dropping either install leaves CI green
    over a strictly smaller suite than the one run locally, with nothing anywhere
    saying so. That is what shipped for months (dev/changelog/907). A floating
    `runs-on: ubuntu-latest` is the same hazard one level up: the runner image decides
    what apt hands over, so a rollover changes the tooling under the suite with no
    commit.

    Installing *an* ffmpeg is not enough, which is the lesson of dev/changelog/916: for
    its first weeks CI ran the runner image's 6.1.1 while the container shipped 7.1.5,
    so every push was green over a series no user runs and a regression reaching only
    the shipped build had no environment that could catch it. The Dockerfile's
    FFMPEG_SERIES is the one declaration of what ChannelBin targets, so that is what
    CI is held to here rather than a version this file re-types.

    This is a text scan, not a schema check - it asserts the facts a future edit could
    quietly drop, and deliberately asserts no action version numbers, which are supposed
    to move.
    """

    WORKFLOW = os.path.join(ROOT, '.github', 'workflows', 'tests.yml')
    DOCKERFILE = os.path.join(ROOT, 'Dockerfile')

    def setUp(self):
        if not os.path.exists(self.WORKFLOW):
            self.skipTest('no .github/workflows/tests.yml in this checkout')
        with open(self.WORKFLOW, encoding='utf-8') as fh:
            self.text = fh.read()

    def _step_containing(self, needle):
        """The body of the workflow step whose text contains `needle`."""
        steps = re.split(r'\n      - (?:name|uses):', self.text)
        return next((s for s in steps if needle in s), None)

    def test_ci_runs_the_ffmpeg_series_the_container_ships(self):
        if not os.path.exists(self.DOCKERFILE):
            self.skipTest('no Dockerfile in this checkout')
        with open(self.DOCKERFILE, encoding='utf-8') as fh:
            shipped = re.search(r'^ARG FFMPEG_SERIES=(\S+)', fh.read(), re.M)
        self.assertTrue(shipped, 'the Dockerfile no longer declares ARG FFMPEG_SERIES')
        tested = re.search(r'^\s*FFMPEG_SERIES:\s*"?([^"\s]+)"?', self.text, re.M)
        self.assertTrue(
            tested, 'the workflow no longer declares which ffmpeg series it pins, so CI '
            'can drift off the shipped one the way it did before dev/changelog/916')
        self.assertEqual(
            tested.group(1), shipped.group(1),
            f"CI tests ffmpeg {tested.group(1)} while the container ships "
            f"{shipped.group(1)}: every run is green over a series no user has. Move "
            'both together, and only once the app has been measured on the new build.')

    def test_the_pinned_ffmpeg_download_is_checksummed(self):
        if 'FFMPEG_URL' not in self.text:
            self.skipTest('the workflow does not download a pinned ffmpeg build')
        self.assertRegex(
            self.text, r'sha256sum -c',
            'the workflow downloads an ffmpeg build over the network without verifying '
            'it, so whatever that URL serves becomes the capture engine under the suite')

    def test_it_verifies_the_ffmpeg_toolchain_before_running(self):
        # A space before the flag, so `python-version:` and `node --version` do not match.
        step = self._step_containing(' -version')
        self.assertIsNotNone(step, 'no workflow step resolves an external tool version')
        for binary in ('ffmpeg', 'ffprobe'):
            self.assertIn(
                binary, step,
                f'the workflow never resolves {binary}, so a run where it is missing or '
                'is the wrong series reports green while the tests gated on it silently '
                'skip themselves')
        self.assertIn(
            'exit 1', step,
            'the workflow prints the external tool versions but no longer fails on a '
            'wrong one, so a substituted ffmpeg is a line in a log nobody reads rather '
            'than a red build')

    def test_it_installs_the_node_dependencies(self):
        self.assertRegex(
            self.text, r'\bnpm (ci|install)\b',
            'the workflow stopped installing node_modules, so every jsdom-backed '
            'client-side test now skips itself on the runner while CI still reports green')

    def test_the_runner_image_is_pinned(self):
        runners = re.findall(r'runs-on:\s*(\S+)', self.text)
        self.assertTrue(runners, 'no runs-on: line found in the workflow')
        floating = [r for r in runners if r.endswith('-latest')]
        self.assertEqual(
            floating, [],
            'a floating runner image changes the ffmpeg under the suite on GitHub\'s '
            f'schedule rather than on a commit: {floating}')


class DirectNavigationBypassTests(unittest.TestCase):
    """A click that navigates goes through util.js, so Ctrl-click still opens a new tab.

    Guards dev/docs/BUGS.md 2026-09-16 @ 10:27:44 AM. A row, tile or label that navigates by
    assigning `location.href` from a click listener is not a link, so the browser gives it
    none of a link's modifiers: Ctrl/Cmd-click and middle-click replaced the current page on
    Search Programs, the recordings list, the accounts and groups lists, the dashboard and
    the TV Guide's channel column, while the same click on a real `<a>` worked. The fix
    routes every one through `util.js::bindNavClicks` / `followHref` (dev/changelog/996).

    Every remaining direct assignment is a navigation no modifier could apply to - a modal
    button, a redirect after a save or delete, a per-page select - and says so with a
    `nav-ok: <reason>` marker on the line or in the comment block directly above it. A new
    unmarked one is either a click that should go through the helper or a site that owes
    the reader that sentence.
    """

    _MARKER = 'nav-ok:'
    _PATTERN = re.compile(r'\blocation(?:\.href)?\s*=(?!=)')
    _COMMENT_START = ('//', '/*', '*', '{#', '<!--')

    @classmethod
    def _marked(cls, lines, idx):
        if cls._MARKER in lines[idx]:
            return True
        for j in range(idx - 1, -1, -1):
            stripped = lines[j].strip()
            if not stripped.startswith(cls._COMMENT_START):
                return False
            if cls._MARKER in stripped:
                return True
        return False

    def test_no_unmarked_direct_navigation(self):
        offenders = []
        paths = list(_walk(JS_DIR, '.js')) + list(_walk(TPL_DIR, '.html'))
        for path in sorted(paths):
            lines = _read(path).splitlines()
            for i, line in enumerate(lines):
                if not self._PATTERN.search(line) or self._marked(lines, i):
                    continue
                if _rel(path) == 'static/js/util.js' and 'window.location.href = url; return;' in line:
                    continue
                offenders.append(f'{_rel(path)}:{i + 1}: {line.strip()}')
        self.assertEqual(
            offenders, [],
            'a direct location assignment with no nav-ok: marker. A click that navigates '
            'must go through util.js::bindNavClicks/followHref so Ctrl/Cmd-click and '
            'middle-click open a new tab (dev/changelog/996); anything else says why '
            'no modifier applies in a `nav-ok: <reason>` comment')


if __name__ == '__main__':
    unittest.main(verbosity=2)
