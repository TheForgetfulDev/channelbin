"""The Settings page's raw config.yaml editor: what its Save refuses, and that its Validate
button gives the same answer (dev/docs/BUGS.md 2026-09-19 @ 04:57:41 PM, dev/changelog/1044)."""
import html
import os
import re
import sys
import unittest

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tests.support.app import make_test_app  # noqa: E402
from app import config as cfgmod  # noqa: E402

# One text per kind of problem, each paired with whether Save may write it.
CASES = [
    ('recording: [1\n', False),                                  # does not parse
    ('- just\n- a list\n', False),                               # not a mapping
    ('recording: 5\n', False),                                   # a section as a scalar
    ('recording:\n  retention_delete_file: "maybe"\n', False),   # bool leaf given text
    ('channel_testing:\n  window:\n    start: 1:30\n', False),   # YAML 1.1 reads 1:30 as 90
    ('search:\n  page_size: 75\n', False),                       # a rule a field route enforces
    ('recording:\n  no_such_setting: 1\n', True),                # unknown key: warned, saved
    ('flask:\n  port: 5001\nflask:\n  port: 5002\n', True),      # duplicate key: warned, saved
    ('recording:\n  retention_delete_file: true\n', True),
]


class RawYamlSaveTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        page = self.t.client.get('/settings').get_data(as_text=True)
        self.tok = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)

    def _file(self):
        with open(cfgmod._CONFIG_PATH) as f:  # direct-config-read: asserting on stored bytes
            return f.read()

    def _save(self, text):
        return self.t.client.post('/settings', data={'config_yaml': text, 'csrf_token': self.tok})

    def _problems_region(self, page):
        m = re.search(r'<div id="yaml-check"[^>]*>(.*?)</div>', page, re.S)
        self.assertIsNotNone(m, 'the refused save did not render the problem list')
        return html.unescape(m.group(1))

    def test_a_section_written_as_a_scalar_is_refused_and_every_page_still_loads(self):
        """`recording: 5` used to save, after which every page - Settings included - 500'd,
        so the mistake could not be undone from the UI."""
        before = self._file()
        resp = self._save('recording: 5\n')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._file(), before)
        self.assertIn('recording', self._problems_region(resp.get_data(as_text=True)))
        for url in ('/', '/settings'):
            self.assertEqual(self.t.client.get(url).status_code, 200, url)

    def test_a_leaf_of_the_wrong_type_is_refused(self):
        before = self._file()
        resp = self._save('recording:\n  retention_delete_file: "maybe"\n')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._file(), before)
        self.assertIn('recording.retention_delete_file',
                      self._problems_region(resp.get_data(as_text=True)))

    def test_a_refused_save_keeps_the_typed_text_and_opens_the_yaml_tab(self):
        """A redirect used to reload the editor from disk, discarding the edit."""
        text = 'recording: [1\n# a long edit the user would not want to retype\n'
        page = self._save(text).get_data(as_text=True)
        area = re.search(r'<textarea name="config_yaml"[^>]*>(.*?)</textarea>', page, re.S)
        self.assertEqual(html.unescape(area.group(1)), text)
        self.assertRegex(page, r'class="tab active"[^>]*data-tab="yaml"')
        self.assertNotRegex(page, r'<div id="pane-yaml"[^>]*display:none')

    def test_a_parse_error_names_its_line_and_never_echoes_the_offending_text(self):
        page = self._save('flask:\n  secret_key: [hunter2-typed-secret\n').get_data(as_text=True)
        region = self._problems_region(page)
        self.assertIn('Line 3', region)
        self.assertNotIn('hunter2-typed-secret', region)

    def test_a_warning_does_not_block_the_save(self):
        resp = self._save('recording:\n  no_such_setting: 1\n')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('no_such_setting', self._file())


class ValidateAgreesWithSaveTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        page = self.t.client.get('/settings').get_data(as_text=True)
        self.tok = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)

    def test_validate_and_save_give_the_same_answer_for_every_case(self):
        for text, saveable in CASES:
            with self.subTest(text=text):
                v = self.t.client.post('/api/settings/validate', json={'text': text},
                                       headers={'X-CSRFToken': self.tok})
                self.assertEqual(v.status_code, 200)
                body = v.get_json()
                self.assertTrue(body['success'])
                self.assertEqual(body['valid'], saveable, body['problems'])
                saved = self.t.client.post('/settings', data={'config_yaml': text,
                                                              'csrf_token': self.tok})
                self.assertEqual(saved.status_code == 302, saveable)

    def test_validate_writes_nothing(self):
        with open(cfgmod._CONFIG_PATH) as f:  # direct-config-read: asserting on stored bytes
            before = f.read()
        self.t.client.post('/api/settings/validate',
                           json={'text': 'recording:\n  retention_delete_file: true\n'},
                           headers={'X-CSRFToken': self.tok})
        with open(cfgmod._CONFIG_PATH) as f:  # direct-config-read: asserting on stored bytes
            self.assertEqual(f.read(), before)

    def test_validate_requires_text(self):
        v = self.t.client.post('/api/settings/validate', json={},
                               headers={'X-CSRFToken': self.tok})
        self.assertEqual(v.status_code, 400)
        self.assertIn('error', v.get_json())


class DefaultsAreCleanTests(unittest.TestCase):

    def test_the_editor_s_own_dump_of_the_defaults_has_no_problems(self):
        """What the editor shows on a fresh install must validate clean, or every user
        starts from a page that complains about text they never wrote."""
        text = yaml.dump(cfgmod.mask_config(cfgmod._DEFAULTS), default_flow_style=False,
                         sort_keys=False)
        self.assertEqual(cfgmod.check_config_text(text)[1], [])


if __name__ == '__main__':
    unittest.main()
