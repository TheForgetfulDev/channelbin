"""Tier 0 - `fieldRow()` in static/js/util.js, the one row of the settings anatomy.

Two defects, both shipped at once in the group Settings modal's "this group records from
nobody" state (dev/docs/BUGS.md 2026-09-21), and neither reachable from Python because the
markup is built in the browser:

  * The row passed a `control` and no `meta`, and the helper interpolated the missing key
    straight into the template - so the modal rendered the literal word `undefined` as its
    own paragraph, directly under the sentence explaining the state.
  * That same row asked for `full`, which is the shape for a row with NO control. `full`
    turns the flex row into a block, so the 220px right-hand control column drops to the
    next line and keeps its width, leaving the button floating in the middle of the panel
    with nothing beside it. `stack` is the shape for a control too wide for that column;
    the helper's own comment in util.js says the two are not interchangeable.

The first class is fixed in the helper, so no future caller can reintroduce it. The second
cannot be - `full` plus `control` is a legal object - so it is caught here as a scan of the
call sites instead.

  python3 -m unittest tests.test_field_row_js
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTIL_JS = os.path.join(REPO, 'static', 'js', 'util.js')
JS_DIR = os.path.join(REPO, 'static', 'js')

# argv: [util.js path, a JSON object to hand fieldRow]. util.js installs a global fetch
# wrapper and a few delegated listeners at load; none of it is under test, but all of it
# has to not throw for the file to evaluate. `location` deliberately holds no URL string -
# tests/support/netguard.py refuses to spawn a child whose argv contains one.
_HARNESS = """
const fs = require('fs');
global.document = {
  querySelector: () => null,
  addEventListener() {},
  createElement: () => ({ style: {}, classList: { add() {}, remove() {} } }),
};
global.window = { fetch: () => Promise.resolve(), addEventListener() {},
                  matchMedia: () => ({ matches: false, addEventListener() {} }) };
global.location = {};
const src = fs.readFileSync(process.argv[1], 'utf8');
const fieldRow = new Function(src + '\\nreturn fieldRow;')();
console.log(JSON.stringify(fieldRow(JSON.parse(process.argv[2]))));
"""


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
class MissingKeyTests(unittest.TestCase):
    """A row that omits a half renders nothing there, never the word `undefined`."""

    def render(self, opts):
        proc = subprocess.run(['node', '-e', _HARNESS, UTIL_JS, json.dumps(opts)],
                              capture_output=True, text=True, cwd=REPO, timeout=60)
        if proc.returncode != 0:
            self.fail(f'node failed rendering {opts!r}:\n{proc.stderr}')
        return json.loads(proc.stdout)

    def test_a_row_with_no_meta_renders_no_undefined(self):
        html = self.render({'label': 'Format strategy', 'control': '<button>Go</button>'})
        self.assertNotIn('undefined', html)
        self.assertIn('Format strategy', html)
        self.assertIn('<button>Go</button>', html)

    def test_a_row_with_no_label_renders_no_undefined(self):
        html = self.render({'meta': 'An explanation with no setting name over it.'})
        self.assertNotIn('undefined', html)
        self.assertIn('An explanation', html)

    def test_a_row_with_neither_renders_no_undefined(self):
        self.assertNotIn('undefined', self.render({'control': '<input>'}))

    def test_both_halves_still_render_when_supplied(self):
        """The guard is an empty-string default, not a swallow - a real value still lands."""
        html = self.render({'label': 'Name', 'meta': 'How it shows up.', 'control': '<input>'})
        self.assertIn('>Name<', html)
        self.assertIn('How it shows up.', html)


def _field_row_call_sites():
    """Yield (path, line, body-source) for every `fieldRow({...})` call in static/js.

    Brace-matched rather than regexed: the option objects run to a dozen lines and carry
    nested template literals with braces of their own.
    """
    for name in sorted(os.listdir(JS_DIR)):
        if not name.endswith('.js'):
            continue
        path = os.path.join(JS_DIR, name)
        with open(path, encoding='utf-8') as fh:
            src = fh.read()
        for m in re.finditer(r'fieldRow\(\{', src):
            i = m.end() - 1
            depth = 0
            j = i
            while j < len(src):
                if src[j] == '{':
                    depth += 1
                elif src[j] == '}':
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            yield name, src[:m.start()].count('\n') + 1, src[i:j + 1]


class FullRowHasNoControlTests(unittest.TestCase):
    """`full` is the no-control shape. Passing a control with it strands the control."""

    def test_call_sites_exist_to_be_scanned(self):
        """Meta-assertion: a scan that found nothing would pass forever."""
        self.assertGreater(len(list(_field_row_call_sites())), 20)

    def test_no_call_site_passes_full_with_a_control(self):
        offenders = []
        for name, line, body in _field_row_call_sites():
            keys = set(re.findall(r'[{\s,]([a-zA-Z_]\w*)\s*:', body))
            if 'full' in keys and 'control' in keys:
                offenders.append(f'static/js/{name}:{line}')
        self.assertEqual(offenders, [], 'fieldRow `full` rows must have no control - use '
                                        '`stack` for a control too wide for the right-hand '
                                        f'column: {offenders}')


if __name__ == '__main__':
    unittest.main()
