"""The icon registry: one MDI icon per meaning, drawn one way (dev/changelog/1320).

`app/icons.py` maps each meaning to one Material Design Icons icon; `tools/build_icons.py`
generates `static/icons/mdi-sprite.svg` from it and the pinned `@mdi/js` package; templates
draw with the `icon()` Jinja global and scripts with `util.js::icon()`. These hold that:

  * every name a template or script asks for exists, and no two meanings share a picture -
    one glyph meaning two things (`▶` for both Test and Resume) is what the registry ends;
  * the checked-in sprite is exactly what the generator writes, so a registry edit without
    a rebuild cannot ship, and no path is ever hand-copied;
  * the Jinja and JS calls draw byte-identical markup;
  * no file gains a raw icon glyph or an inline `<svg>` - today's files are frozen at their
    current counts until the app-wide conversion takes them to zero.

  python3 -m unittest tests.test_icon_registry
"""
import html.entities
import importlib.util
import json
import os
import re
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL_DIR = os.path.join(REPO, 'templates')
JS_DIR = os.path.join(REPO, 'static', 'js')
CSS_DIR = os.path.join(REPO, 'static', 'css')
SPRITE = os.path.join(REPO, 'static', 'icons', 'mdi-sprite.svg')
NODE_MODULES = os.path.join(REPO, 'node_modules')
MDI_JS = os.path.join(NODE_MODULES, '@mdi', 'js', 'mdi.js')
JSDOM = os.path.join(NODE_MODULES, 'jsdom')
HARNESS = os.path.join(REPO, 'tests', 'support', 'icons.mjs')


def _walk(root, exts):
    for dp, _dn, fns in os.walk(root):
        for fn in sorted(fns):
            if fn.endswith(exts):
                yield os.path.join(dp, fn)


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _rel(path):
    return os.path.relpath(path, REPO)


def _build_tool():
    spec = importlib.util.spec_from_file_location(
        'build_icons', os.path.join(REPO, 'tools', 'build_icons.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# A call of the registry's icon(), not nav_icon() or a method named icon.
_CALL_RE = re.compile(r"""(?<![\w.$])icon\(\s*(['"`])([^'"`$]*)\1""")


class RegistryShapeTests(unittest.TestCase):
    def test_no_two_meanings_share_an_mdi_icon(self):
        from app.icons import ICONS
        seen = {}
        for meaning, mdi in ICONS.items():
            seen.setdefault(mdi, []).append(meaning)
        shared = {mdi: ms for mdi, ms in seen.items() if len(ms) > 1}
        self.assertEqual(shared, {}, 'one picture would mean two things; give each meaning '
                                     'its own MDI icon in app/icons.py')

    def test_meanings_are_sprite_safe_ids(self):
        from app.icons import ICONS
        bad = [m for m in ICONS if not re.fullmatch(r'[a-z][a-z0-9-]*', m)]
        self.assertEqual(bad, [], 'a meaning becomes an id and a class-free attribute value; '
                                  'keep it lowercase words joined by hyphens')

    def test_every_name_a_template_or_script_draws_exists(self):
        from app.icons import ICONS
        used, unknown = [], []
        for path in list(_walk(TPL_DIR, '.html')) + list(_walk(JS_DIR, '.js')):
            for m in _CALL_RE.finditer(_read(path)):
                used.append(m.group(2))
                if m.group(2) not in ICONS:
                    unknown.append(f'{_rel(path)}: icon({m.group(2)!r})')
        self.assertEqual(unknown, [], 'icon() asked for a meaning app/icons.py does not have')
        # The scan is not vacuous: the guide and the channel page draw `search` through it.
        self.assertIn('search', used)

    def test_record_is_red(self):
        css = _read(os.path.join(CSS_DIR, 'style.css'))
        self.assertRegex(css, r'\.icon\[data-icon="record"\]\s*\{[^}]*color:\s*var\(--live\)')


class SpriteTests(unittest.TestCase):
    @unittest.skipIf(not os.path.isdir(NODE_MODULES), 'node_modules not installed (npm ci)')
    def test_sprite_is_exactly_what_the_registry_generates(self):
        from app.icons import ICONS
        self.assertTrue(os.path.exists(MDI_JS),
                        'node_modules has no @mdi/js - run `npm ci` in the repo root')
        expected = _build_tool().build(ICONS, _read(MDI_JS))
        self.assertTrue(
            _read(SPRITE) == expected,
            'static/icons/mdi-sprite.svg is not what app/icons.py and the installed @mdi/js '
            'generate - run `python3 tools/build_icons.py` and commit the result')

    def test_sprite_has_one_symbol_per_meaning(self):
        from app.icons import ICONS
        ids = re.findall(r'<symbol id="i-([^"]+)"', _read(SPRITE))
        self.assertEqual(ids, list(ICONS))

    def test_build_refuses_a_name_mdi_does_not_have(self):
        src = '// Material Design Icons v1.0.0\nexport var mdiPlus = "M1 1Z";\n'
        tool = _build_tool()
        self.assertIn('<symbol id="i-add"', tool.build({'add': 'plus'}, src))
        with self.assertRaises(KeyError):
            tool.build({'add': 'plus', 'gone': 'no-such-icon'}, src)

    def test_the_license_ships_beside_the_sprite(self):
        self.assertIn('Apache', _read(os.path.join(REPO, 'static', 'icons', 'LICENSE')))

    def test_the_package_is_pinned_exactly(self):
        with open(os.path.join(REPO, 'package.json'), encoding='utf-8') as fh:
            pin = json.load(fh)['devDependencies']['@mdi/js']
        self.assertRegex(pin, r'^\d+\.\d+\.\d+$', 'a range would let `npm install` move the '
                                                   'sprite\'s source under it')
        self.assertIn(f'v{pin}.', _read(SPRITE))


class JinjaIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.support import make_test_app
        cls.t = make_test_app()

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def test_markup(self):
        icon = self.t.app.jinja_env.globals['icon']
        with self.t.app.test_request_context('/'):
            self.assertEqual(
                str(icon('record')),
                '<svg class="icon" data-icon="record" aria-hidden="true">'
                '<use href="/static/icons/mdi-sprite.svg#i-record"></use></svg>')
            self.assertIn('class="icon guide-icon"', str(icon('search', 'guide-icon')))

    def test_unknown_name_fails_the_render(self):
        icon = self.t.app.jinja_env.globals['icon']
        with self.t.app.test_request_context('/'):
            with self.assertRaises(KeyError):
                icon('no-such-meaning')


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class JsMatchesJinjaTests(unittest.TestCase):
    CALLS = [['record', ''], ['search', 'guide-icon'], ['more', 'a b'], ['nav-settings', '']]

    @classmethod
    def setUpClass(cls):
        from tests.support import make_test_app
        t = make_test_app()
        try:
            page = t.client.get('/tags').get_data(as_text=True)
            icon = t.app.jinja_env.globals['icon']
            with t.app.test_request_context('/'):
                cls.jinja = [str(icon(n, c)) for n, c in cls.CALLS]
        finally:
            t.cleanup()
        meta = re.search(r'<meta name="icon-sprite"[^>]*>', page)
        assert meta, 'base.html no longer serves <meta name="icon-sprite">'
        calls = cls.CALLS + [['no-such-meaning', '']]
        proc = subprocess.run([shutil.which('node'), HARNESS, REPO, meta.group(0), json.dumps(calls)],
                              capture_output=True, text=True, cwd=REPO, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr}')
        cls.js = json.loads(proc.stdout)

    def test_no_script_error(self):
        self.assertEqual(self.js['errors'], [])

    def test_js_draws_exactly_what_jinja_draws(self):
        self.assertEqual(self.js['drawn'][:len(self.CALLS)], self.jinja)

    def test_unknown_name_draws_nothing_and_says_so(self):
        self.assertEqual(self.js['drawn'][-1], '')
        self.assertTrue(any('no-such-meaning' in e for e in self.js['consoleErrors']),
                        self.js['consoleErrors'])


# Raw icon glyphs: arrows, technical symbols, geometric shapes, miscellaneous symbols,
# dingbats, emoji, the ⋮ ⋯ menus and the × close. Typography (… — · •) is text, not an icon.
_GLYPH = re.compile('[\u00d7\u2190-\u21ff\u22ee\u22ef\u2300-\u23ff\u25a0-\u25ff\u2600-\u27bf'
                    '\u29c9\u2b00-\u2bff\U0001F300-\U0001FAFF]')
_NUM_ENTITY = re.compile(r'&#(?:(\d+)|[xX]([0-9a-fA-F]+));')
_NAMED_ENTITY = re.compile(r'&([A-Za-z][A-Za-z0-9]*);')
_JS_ESCAPE = re.compile(r'\\u(?:([0-9a-fA-F]{4})|\{([0-9a-fA-F]+)\})')
_CSS_ESCAPE = re.compile(r'\\([0-9a-fA-F]{4,6})')
_SVG = re.compile(r'<svg\b')
MARKER = 'icon-ok:'


def _decoded(line, ext):
    """Every character the line will put on screen: literals, entities and escapes."""
    out = [line]
    for m in _NUM_ENTITY.finditer(line):
        out.append(chr(int(m.group(1)) if m.group(1) else int(m.group(2), 16)))
    for m in _NAMED_ENTITY.finditer(line):
        out.append(html.entities.html5.get(m.group(1) + ';', ''))
    if ext in ('.js', '.html'):
        for m in _JS_ESCAPE.finditer(line):
            out.append(chr(int(m.group(1) or m.group(2), 16)))
    if ext in ('.css', '.html'):
        for m in _CSS_ESCAPE.finditer(line):
            out.append(chr(int(m.group(1), 16)))
    return ''.join(out)


def scan_raw_icons():
    """{path: (glyphs, inline svgs)} for every file that draws an icon outside the registry."""
    found = {}
    for path in (list(_walk(TPL_DIR, '.html')) + list(_walk(JS_DIR, '.js'))
                 + list(_walk(CSS_DIR, '.css'))):
        ext = os.path.splitext(path)[1]
        glyphs = svgs = 0
        for line in _read(path).splitlines():
            if MARKER in line:
                continue
            glyphs += len(_GLYPH.findall(_decoded(line, ext)))
            svgs += len(_SVG.findall(line))
        if glyphs or svgs:
            found[_rel(path)] = (glyphs, svgs)
    return found


class NoNewRawIconTests(unittest.TestCase):
    """No file gains a raw icon glyph or an inline <svg>; an icon is drawn with icon().

    ALLOWANCE freezes every file that drew icons another way when the registry shipped, at
    (glyphs, inline svgs). It only goes down: a file under its allowance fails too, so the
    number is lowered in the same change that converts something, and the backlog's
    app-wide conversion takes it to an empty dict. The channel search page's own emoji
    (templates/channels/search.html, static/js/channel-search.js) are a granted exception
    (DESIGN.md §4), counted here like everything else so the conversion has to ask about
    them rather than sweep them.

    A line that legitimately draws one carries `icon-ok: <reason>` - a data chart, the
    registry's own markup.
    """

    ALLOWANCE = {
        'static/css/guide.css': (4, 0),
        'static/css/style.css': (6, 0),
        'static/js/account-modal.js': (0, 2),
        'static/js/account-stats.js': (4, 0),
        'static/js/auto-rule-modal.js': (3, 0),
        'static/js/channel-detail.js': (2, 0),
        'static/js/channel-search.js': (61, 0),
        'static/js/check-modal.js': (1, 0),
        'static/js/dashboard.js': (4, 0),
        'static/js/dropdown.js': (1, 0),
        'static/js/filename-designer.js': (3, 0),
        'static/js/filter-bar.js': (6, 0),
        'static/js/group-create-flow.js': (3, 0),
        'static/js/group-detail.js': (31, 0),
        'static/js/group-modal.js': (9, 0),
        'static/js/group-promote-modal.js': (1, 0),
        'static/js/groups.js': (4, 0),
        'static/js/guide.js': (23, 0),
        'static/js/hide-rules.js': (4, 0),
        'static/js/manual_record.js': (2, 0),
        'static/js/nav-alerts.js': (4, 0),
        'static/js/readiness.js': (5, 0),
        'static/js/recordings.js': (7, 0),
        'static/js/settings.js': (0, 2),
        'static/js/tag-modal.js': (1, 0),
        'static/js/util.js': (8, 0),
        'templates/_account_providers.html': (2, 0),
        'templates/_macros.html': (1, 2),
        'templates/_record_modal.html': (5, 0),
        'templates/account_detail.html': (13, 0),
        'templates/accounts.html': (1, 0),
        'templates/alerts.html': (3, 0),
        'templates/alerts_ignored.html': (2, 0),
        'templates/base.html': (12, 3),
        'templates/channels/_timeline.html': (4, 0),
        'templates/channels/detail.html': (37, 0),
        'templates/channels/group_detail.html': (17, 0),
        'templates/channels/groups.html': (15, 0),
        'templates/channels/guide_compare.html': (1, 0),
        'templates/channels/hide_rules.html': (1, 0),
        'templates/channels/search.html': (14, 0),
        'templates/dashboard.html': (3, 0),
        'templates/epg_source_review.html': (4, 0),
        'templates/get_started.html': (6, 0),
        'templates/guide.html': (9, 0),
        'templates/health_check_profiles.html': (2, 0),
        'templates/index.html': (9, 0),
        'templates/jobs.html': (1, 0),
        'templates/logs.html': (4, 0),
        'templates/maintenance.html': (2, 0),
        'templates/notifications_settings.html': (2, 0),
        'templates/profiles.html': (1, 0),
        'templates/recording_detail.html': (42, 0),
        'templates/rule_detail.html': (4, 0),
        'templates/rules.html': (2, 0),
        'templates/settings.html': (3, 0),
        'templates/tags.html': (1, 0),
    }

    def test_no_file_draws_more_raw_icons_than_its_allowance(self):
        found = scan_raw_icons()
        over = [f'{p}: {found[p]} > allowed {self.ALLOWANCE.get(p, (0, 0))}'
                for p in sorted(found)
                if any(n > a for n, a in zip(found[p], self.ALLOWANCE.get(p, (0, 0))))]
        self.assertEqual(over, [], 'a raw icon glyph or inline <svg> was added; draw it with '
                                   "icon('<meaning>') from app/icons.py instead "
                                   '(glyphs, svgs):\n  ' + '\n  '.join(over))

    def test_allowance_goes_down_when_a_file_is_converted(self):
        found = scan_raw_icons()
        under = [f'{p}: allowed {a}, now {found.get(p, (0, 0))}'
                 for p, a in sorted(self.ALLOWANCE.items())
                 if found.get(p, (0, 0)) != a
                 and all(n <= x for n, x in zip(found.get(p, (0, 0)), a))]
        self.assertEqual(under, [], 'lower these allowances to what the file draws now (a '
                                    'file at (0, 0) leaves the dict), so nothing can creep '
                                    'back in:\n  ' + '\n  '.join(under))

    def test_scan_sees_each_spelling(self):
        line = '&#8943; &times; \u25be \\u2715 <svg'
        self.assertEqual(len(_GLYPH.findall(_decoded(line, '.js'))), 4)
        self.assertEqual(len(_GLYPH.findall(_decoded('&hellip; \u2014 \u00b7 1x2', '.html'))), 0)
        self.assertEqual(len(_GLYPH.findall(_decoded("content: '\\25BE';", '.css'))), 1)


if __name__ == '__main__':
    unittest.main()
