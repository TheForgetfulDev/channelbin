"""config.example.yaml and README's configuration table must agree with app/config.py::_DEFAULTS.

Three places describe this app's configuration - the built-in defaults, the shipped template
users are told to copy, and the README table - and they had drifted apart in both directions
(dev/changelog/725). The template silently changed seven values relative to the defaults, so
following the README's `cp config.example.yaml config.yaml` altered behavior with nothing to
notice it by, and 37 keys that exist in _DEFAULTS were absent from the file the README calls
the annotated reference, the whole `auth.*` block among them.

Prose did not hold these three in sync, so this is the guard: every key the template sets must
carry exactly the built-in default, every built-in key must appear in the template unless it is
listed below as a deliberate omission, and every default quoted in the README table must be the
real one.
"""
import os
import re
import unittest

import yaml

from app.config import _DEFAULTS, _flatten

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATE = os.path.join(_REPO_ROOT, 'config.example.yaml')
_README = os.path.join(_REPO_ROOT, 'README.md')

# Keys _DEFAULTS defines that config.example.yaml deliberately does not set. Adding to this
# list is a decision about what a normal install should be able to configure, not a way to
# quiet a failing test: a key belongs here only when setting it wrongly breaks startup rather
# than a feature, or when its default is a path derived from wherever the app is installed and
# a literal value in a shipped template would be wrong for everyone.
DELIBERATELY_OMITTED = {
    # Serves dev/mockups/, a directory a public checkout does not have (dev/changelog/714).
    'flask.serve_mockups',
    # Raw-YAML-only; the default lands in instance/ beside the app.
    'flask.pidfile_path',
    # Installation-derived paths. Both appear in the template as commented-out examples,
    # which is what a user needs to see, but neither can ship an uncommented literal.
    'database.path',
    'logging.file',
}


def _template():
    with open(_TEMPLATE) as f:
        return yaml.safe_load(f)


class TemplateMatchesDefaultsTests(unittest.TestCase):

    def test_every_template_value_equals_the_built_in_default(self):
        """Copying the template must be a no-op until the user edits it."""
        defaults = dict(_flatten(_DEFAULTS))
        mismatched = []
        for path, value in _flatten(_template()):
            self.assertIn(path, defaults,
                          f'config.example.yaml sets {path}, which is not a key in _DEFAULTS')
            if defaults[path] != value:
                mismatched.append(f'  {path}: template={value!r} default={defaults[path]!r}')
        self.assertEqual(mismatched, [],
                         'config.example.yaml departs from the built-in defaults, so copying it '
                         'silently changes behavior:\n' + '\n'.join(mismatched))

    def test_every_default_key_is_in_the_template_or_deliberately_omitted(self):
        template_paths = {path for path, _ in _flatten(_template())}
        missing = sorted(
            path for path, _ in _flatten(_DEFAULTS)
            if path not in template_paths and path not in DELIBERATELY_OMITTED)
        self.assertEqual(missing, [],
                         'these keys exist in _DEFAULTS but config.example.yaml does not '
                         'document them - add them to the template, or to '
                         'DELIBERATELY_OMITTED with the reason:\n  ' + '\n  '.join(missing))

    def test_the_omission_list_names_only_real_keys(self):
        """A stale allowlist entry silently exempts nothing and hides the next omission."""
        default_paths = {path for path, _ in _flatten(_DEFAULTS)}
        stale = sorted(DELIBERATELY_OMITTED - default_paths)
        self.assertEqual(stale, [],
                         f'DELIBERATELY_OMITTED names keys that no longer exist: {stale}')

    def test_the_auth_block_is_present(self):
        """README and SECURITY.md both tell users to set these in config.yaml, so they have to
        be in the file those documents tell them to copy."""
        template_paths = {path for path, _ in _flatten(_template())}
        for key in ('auth.enabled', 'auth.cookie_secure', 'auth.password_hash',
                    'auth.session_timeout_minutes'):
            self.assertIn(key, template_paths,
                          f'{key} is documented as user-settable but is absent from '
                          'config.example.yaml')

    def test_no_secret_is_written_into_the_template(self):
        """The template ships the shape of a secret, never a value."""
        for path, value in _flatten(_template()):
            if path.endswith(('password_hash', 'api_key_hash')) or path.endswith('services.url'):
                self.assertEqual(value, '', f'{path} must ship empty, got {value!r}')


def _readme_config_table_rows():
    """Yield (key, documented_default) for each row of README's Configuration table."""
    with open(_README) as f:
        lines = f.read().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == '## Configuration')
    for line in lines[start:]:
        if line.startswith('## ') and line.strip() != '## Configuration':
            break
        m = re.match(r'^\|\s*`([a-z_.]+)`\s*\|([^|]*)\|', line)
        if m:
            yield m.group(1), m.group(2).strip().strip('`')


def _as_documented(value):
    """Render a _DEFAULTS value the way the README table writes it."""
    if value == '':
        return 'none'
    if isinstance(value, list) and not value:
        return '[]'
    return str(value)


class ReadmeTableMatchesDefaultsTests(unittest.TestCase):

    def test_the_table_is_found_and_populated(self):
        """A parser that silently matches nothing would make every assertion below vacuous."""
        rows = list(_readme_config_table_rows())
        self.assertGreaterEqual(len(rows), 10,
                                f'only found {len(rows)} rows in the README configuration '
                                'table - the parser or the table format has changed')

    def test_every_documented_default_is_the_real_default(self):
        defaults = dict(_flatten(_DEFAULTS))
        wrong = []
        for key, documented in _readme_config_table_rows():
            self.assertIn(key, defaults,
                          f'README documents {key}, which is not a key in _DEFAULTS')
            expected = _as_documented(defaults[key])
            if documented != expected:
                wrong.append(f'  {key}: README={documented!r} default={expected!r}')
        self.assertEqual(wrong, [],
                         'README documents defaults the code does not have, so running with no '
                         'config file behaves differently than documented:\n' + '\n'.join(wrong))


if __name__ == '__main__':
    unittest.main(verbosity=2)
