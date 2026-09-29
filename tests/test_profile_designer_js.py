"""The filename designer as a STEP inside the Recording Profile modal (dev/changelog/1161),
driven in jsdom.

What it must do: open in the profile modal's own panel, never as a second overlay (one
Escape handler, one scroll lock - DESIGN.md 15.1); start from the profile's template and
cleanup lists, or from the global ones when the profile has none; hand the template and
both lists back to the profile form on Use template and write nothing itself, so they are
saved with the profile; leave the form's typed values and buttons working; step back one
level per Escape; and leave the Settings host saving to config as before.

`tests/support/profile_designer.mjs` drives it and reports observations; every assertion
lives here. jsdom computes no layout, so the step at 375px is browser work.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'profile_designer.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is None:
        proc = subprocess.run([shutil.which('node'), HARNESS, REPO],
                              capture_output=True, text=True, cwd=REPO, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
    return _RESULT


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class _Base(unittest.TestCase):
    SCENARIO = ''

    @classmethod
    def setUpClass(cls):
        if not cls.SCENARIO:
            raise unittest.SkipTest('base class - carries no cases')
        cls.obs = _observe()[cls.SCENARIO]
        if 'error' in cls.obs:
            raise AssertionError(f'{cls.SCENARIO} threw:\n{cls.obs["error"]}')

    def test_no_script_errors(self):
        self.assertEqual(self.obs['errors'], [])


class UseTemplateThenSaveTests(_Base):
    SCENARIO = 'use_template_then_save'

    def test_the_step_is_the_same_overlay(self):
        self.assertEqual(self.obs['inStep']['modals'], 1)
        self.assertTrue(self.obs['inStep']['designer'])
        self.assertFalse(self.obs['inStep']['profile_form'])
        self.assertEqual(self.obs['inStep']['title'], 'Filename template')
        self.assertTrue(self.obs['inStep']['xwide'])

    def test_the_step_does_not_claim_to_save(self):
        self.assertEqual(self.obs['inStep']['primary'], 'Use template')

    def test_it_starts_from_the_profiles_own_template_and_lists(self):
        self.assertEqual(self.obs['startTpl'], '{title} P')
        self.assertEqual(self.obs['startRemove'], ['live'])

    def test_use_template_returns_to_the_form_with_its_typed_values(self):
        self.assertEqual(self.obs['back'], self.obs['before'])
        self.assertEqual(self.obs['after']['name'], 'Sports edited')
        self.assertEqual(self.obs['after']['hidden'], '{date} {title}')
        self.assertIn('remove live', self.obs['after']['summary'])

    def test_the_designer_writes_nothing_itself(self):
        self.assertEqual(self.obs['after']['sent_before_profile_save'], [])

    def test_the_profile_save_carries_the_template_and_both_lists(self):
        body = self.obs['save']['body']
        self.assertEqual(self.obs['save']['method'], 'PUT')
        self.assertEqual(body['filename_template'], '{date} {title}')
        self.assertEqual(body['filename_tags_remove'], ['live'])
        self.assertEqual(body['filename_tags_replace'], [])
        self.assertEqual(body['name'], 'Sports edited')


class EscapeLadderTests(_Base):
    SCENARIO = 'escape_ladder'

    def test_escape_in_the_picker_returns_to_the_designer(self):
        self.assertTrue(self.obs['picker']['picker'])
        self.assertTrue(self.obs['designer']['designer'])
        self.assertEqual(self.obs['designer']['modals'], 1)

    def test_escape_in_the_designer_returns_to_the_profile_not_closed(self):
        self.assertTrue(self.obs['profile']['profile_form'])
        self.assertEqual(self.obs['profile']['modals'], 1)
        self.assertFalse(self.obs['profile']['xwide'])

    def test_leaving_without_use_template_keeps_the_old_value(self):
        self.assertEqual(self.obs['hidden'], '{title} P')

    def test_cancel_returns_to_the_profile_too(self):
        self.assertTrue(self.obs['afterCancel']['profile_form'])

    def test_escape_on_the_profile_form_closes_it(self):
        self.assertEqual(self.obs['closed']['modals'], 0)


class BlankProfileTests(_Base):
    SCENARIO = 'blank_profile_starts_from_global'

    def test_the_form_says_it_inherits(self):
        self.assertIn('Uses the global template', self.obs['summary'])
        self.assertTrue(self.obs['clearHidden'])

    def test_the_designer_starts_from_the_global_template_and_lists(self):
        self.assertEqual(self.obs['startTpl'], '{date} - {title} GLOBAL')
        self.assertEqual(self.obs['startRemove'], ['hd'])

    def test_using_it_makes_it_the_profiles_own(self):
        self.assertEqual(self.obs['hidden'], '{date} - {title} GLOBAL')
        self.assertTrue(self.obs['clearShown'])
        body = self.obs['save']['body']
        self.assertEqual(self.obs['save']['method'], 'POST')
        self.assertEqual(body['filename_tags_remove'], ['hd'])


class ClearToGlobalTests(_Base):
    SCENARIO = 'clear_to_global'

    def test_clearing_sends_no_template_and_empty_lists(self):
        self.assertEqual(self.obs['hidden'], '')
        self.assertIn('Uses the global template', self.obs['summary'])
        body = self.obs['save']['body']
        self.assertIsNone(body['filename_template'])
        self.assertEqual(body['filename_tags_remove'], [])


class CloseFromStepTests(_Base):
    SCENARIO = 'close_from_step'

    def test_the_x_closes_everything_and_a_reopen_works(self):
        self.assertEqual(self.obs['closed']['modals'], 0)
        self.assertEqual(self.obs['reopened']['modals'], 1)
        self.assertTrue(self.obs['reopened']['designer'])


class SettingsHostTests(_Base):
    SCENARIO = 'settings_host'

    def test_settings_saves_to_config_and_closes(self):
        self.assertEqual(self.obs['opened']['primary'], 'Save')
        self.assertEqual(self.obs['posts'], ['/api/filename-template'])
        self.assertTrue(self.obs['saved'])
        self.assertEqual(self.obs['afterSave']['modals'], 0)

    def test_escape_in_the_picker_steps_back_and_in_the_designer_closes(self):
        self.assertTrue(self.obs['back']['designer'])
        self.assertEqual(self.obs['back']['modals'], 1)
        self.assertEqual(self.obs['closedByEscape']['modals'], 0)


if __name__ == '__main__':
    unittest.main()
