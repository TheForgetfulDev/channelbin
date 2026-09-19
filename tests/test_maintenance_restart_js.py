"""Tier 0 - the Maintenance page's restart flow, driven in a real DOM.

Guards dev/docs/BUGS.md 2026-09-18 "The restart-wait modal hung forever on a restart
faster than one poll" and "In a container, the Restart confirm never said a restart
policy is required". Invariants:

  (a) The wait modal reloads once a heartbeat reports an instance id other than the one
      the restart POST answered with - even when no heartbeat ever fails.
  (b) While the old id keeps answering it does not reload, and the elapsed-seconds text
      and the 45-second Reload page escape hatch still appear without any failed poll.
  (c) With no id to compare (the POST's connection dropped), a good heartbeat counts only
      after a failed one.
  (d) Inside a container the confirm says a restart policy is required; outside one it
      does not.
  (e) A parked recording is named in the Restart confirm, in the server's own words,
      because the POST never refuses over one; a failed lookup says so rather than
      implying nothing is parked. In the 409 dialog parked rows are listed apart from the
      blocking ones (dev/docs/BUGS.md 2026-09-18, "The restart modal blocked on a parked
      recording").

tests/support/maintenance_restart.mjs drives the shipped util.js + maintenance.js against
the markup the /maintenance route really rendered, scripting the heartbeat answers; every
assertion lives here. The endpoint half (the id itself) is in tests/test_restart_guard.py.

  python3 -m unittest tests.test_maintenance_restart_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'maintenance_restart.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='maintenance_restart_js_')
    try:
        client = t.app.test_client()
        env = {k: v for k, v in os.environ.items() if k != 'CHANNELBIN_DOCKER'}
        with patch.dict(os.environ, env, clear=True):
            bare = client.get('/maintenance').get_data(as_text=True)
        with patch.dict(os.environ, {'CHANNELBIN_DOCKER': '1'}):
            docker = client.get('/maintenance').get_data(as_text=True)
        for name, html in (('page.html', bare), ('page_docker.html', docker)):
            with open(os.path.join(tmp, name), 'w', encoding='utf-8') as f:
                f.write(html)
        proc = subprocess.run(['node', HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
        return _RESULT
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class _Base(unittest.TestCase):
    SCENARIO = ''

    @classmethod
    def setUpClass(cls):
        if not cls.SCENARIO:
            raise unittest.SkipTest('base class - carries the shared cases only')
        cls.obs = _observe()[cls.SCENARIO]
        if isinstance(cls.obs, dict) and 'error' in cls.obs:
            raise AssertionError(f'{cls.SCENARIO} threw in the page:\n{cls.obs["error"]}')

    def test_the_page_ran_without_errors(self):
        self.assertEqual(self.obs['errors'], [])


class FastRestartTests(_Base):
    SCENARIO = 'fast'

    def test_a_changed_id_reloads_without_any_failed_poll(self):
        self.assertNotIn(None, self.obs['heartbeats'])
        self.assertEqual(self.obs['reloads'], 1)
        self.assertEqual(self.obs['status'], 'Back online - reloading...')


class OldProcessStillAnsweringTests(_Base):
    SCENARIO = 'same'

    def test_the_old_id_does_not_reload(self):
        self.assertGreater(self.obs['polls'], 3)
        self.assertEqual(self.obs['reloads'], 0)

    def test_the_seconds_counter_runs_on_successful_polls(self):
        self.assertRegex(self.obs['status'], r'\(\d+s\)$')

    def test_no_reload_button_before_the_slow_threshold(self):
        self.assertFalse(self.obs['reloadButton'])


class SlowRestartTests(_Base):
    SCENARIO = 'slow'

    def test_the_escape_hatch_appears_without_a_failed_poll(self):
        self.assertTrue(self.obs['reloadButton'])
        self.assertEqual(self.obs['reloads'], 0)


class FallbackWithoutAnIdTests(_Base):
    SCENARIO = 'fallback'

    def test_a_good_poll_after_a_drop_reloads(self):
        self.assertEqual(self.obs['reloads'], 1)


class FallbackNoDropTests(_Base):
    SCENARIO = 'fallbackNoDrop'

    def test_good_polls_alone_do_not_reload_without_an_id(self):
        self.assertGreater(self.obs['polls'], 3)
        self.assertEqual(self.obs['reloads'], 0)


class ConfirmOutsideContainerTests(_Base):
    SCENARIO = 'confirmBare'

    def test_no_restart_policy_warning(self):
        self.assertIn('Restart the ChannelBin service now?', self.obs['text'])
        self.assertNotIn('restart policy', self.obs['text'])


class ConfirmInContainerTests(_Base):
    SCENARIO = 'confirmDocker'

    def test_the_restart_policy_requirement_is_stated(self):
        self.assertIn('restart policy', self.obs['text'])
        self.assertIn('stays down', self.obs['text'])

    def test_the_button_is_still_named_by_the_verb(self):
        self.assertTrue(self.obs['button'])



class ConfirmNamesParkedTests(_Base):
    SCENARIO = 'confirmParked'

    def test_the_parked_row_and_its_cost_are_named(self):
        self.assertIn('Parked Game', self.obs['text'])
        self.assertIn('waiting on "Live Match"', self.obs['text'])
        self.assertIn('66% encoded so far', self.obs['text'])
        self.assertTrue(self.obs['button'])


class ConfirmNothingParkedTests(_Base):
    SCENARIO = 'confirmBare'

    def test_no_parked_section_when_nothing_is_parked(self):
        self.assertNotIn('Parked, waiting', self.obs['text'])


class ConfirmParkedLookupFailedTests(_Base):
    SCENARIO = 'confirmParkedLookupFailed'

    def test_the_confirm_still_opens_and_says_it_could_not_check(self):
        self.assertTrue(self.obs['button'])
        self.assertIn('Could not check for parked conversions', self.obs['text'])


class RefusalListsParkedApartTests(_Base):
    SCENARIO = 'refusedWithParked'

    def test_blocking_and_parked_are_separate_lists(self):
        self.assertIn('Work is in flight', self.obs['titles'])
        self.assertEqual(len(self.obs['lists']), 2, self.obs['lists'])
        self.assertIn('Live Match', self.obs['lists'][0])
        self.assertNotIn('Parked Game', self.obs['lists'][0])
        self.assertIn('Parked Game', self.obs['lists'][1])


if __name__ == '__main__':
    unittest.main()
