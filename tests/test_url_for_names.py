"""Tier 2 - every url_for('endpoint') in a template names a real view function.

BUGS.md 2026-06-28: a template called url_for() with an endpoint name that didn't exist,
which raises BuildError → 500 the moment that page renders. The route smoke sweep catches
it only for pages that render far enough to reach the bad call; this test catches every
such reference statically, including ones buried in branches the sweep's seed doesn't hit.

Pure text scan of templates/** - no app request needed, only the built app's
view_functions registry. Runs against a throwaway app (never touches production).

    python3 -m unittest tests.test_url_for_names
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import make_test_app  # noqa: E402

_TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates')

# url_for('name.here'  /  url_for("name") - string-literal first arg only. Dynamic
# first args (url_for(some_var)) can't be checked statically and are skipped.
_URL_FOR = re.compile(r"""url_for\(\s*['"]([a-zA-Z0-9_.]+)['"]""")


def _collect_endpoints():
    """{endpoint_name: [relpath, ...]} for every literal url_for target under templates/."""
    found = {}
    for root, _dirs, files in os.walk(_TEMPLATES_DIR):
        for fn in files:
            if not fn.endswith('.html'):
                continue
            path = os.path.join(root, fn)
            with open(path, encoding='utf-8') as f:
                text = f.read()
            rel = os.path.relpath(path, _TEMPLATES_DIR)
            for name in _URL_FOR.findall(text):
                found.setdefault(name, []).append(rel)
    return found


class UrlForNameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = make_test_app()

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def test_every_template_url_for_target_exists(self):
        registry = set(self.t.app.view_functions)  # includes 'static'
        found = _collect_endpoints()
        self.assertTrue(found, 'expected to find url_for() calls in templates/')
        missing = [
            f"'{name}' (in {', '.join(sorted(set(files)))})"
            for name, files in sorted(found.items())
            if name not in registry
        ]
        self.assertEqual(
            missing, [],
            'template url_for() targets with no matching view function:\n' + '\n'.join(missing))


if __name__ == '__main__':
    unittest.main(verbosity=2)
