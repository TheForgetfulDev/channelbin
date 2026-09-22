"""Tier 0 - the Maintenance page's search-index card, driven in a real DOM.

Guards dev/docs/BUGS.md 2026-09-21 "The Maintenance search-index card sat on READY for the
whole of a rebuild that started after page load". Invariants:

  (a) A change in the readiness /api/nav-status reports makes the card re-read its own
      status, and a rebuild found that way starts the existing 3-second poll.
  (b) The trigger survives the sequence an account sync actually produces - stale, then
      building - where `ready` is false at both steps and only the reason moves. A trigger
      watching the boolean alone fires on the staleness and never sees the rebuild.
  (c) The first payload is a baseline and an unchanged one is not a change: neither buys a
      fetch, because the page's own boot load already answered that question.
  (d) The hook is not a second writer of #search-index-rows. It writes nothing itself, it
      stands off while the poll is running, and stopping the poll stays where it was.
  (e) An absent search block is an absence, not a change.
  (f) A fetch the hook triggered that fails says so on the card.

tests/support/maintenance_index.mjs drives the shipped util.js + maintenance.js against the
markup the /maintenance route really rendered, scripting what the status endpoint answers;
every assertion lives here. The readiness payload itself is built server-side by
app/routes/dashboard.py::_search_readiness_dict.

  python3 -m unittest tests.test_maintenance_index_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'maintenance_index.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='maintenance_index_js_')
    try:
        html = t.app.test_client().get('/maintenance').get_data(as_text=True)
        with open(os.path.join(tmp, 'page.html'), 'w', encoding='utf-8') as f:
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


class BaselinePayloadTests(_Base):
    SCENARIO = 'baseline'

    def test_the_hook_is_registered_on_this_page(self):
        self.assertTrue(self.obs['hookDefined'])

    def test_the_first_payload_costs_no_extra_fetch(self):
        self.assertEqual(self.obs['afterBoot'], 1)
        self.assertEqual(self.obs['afterFirst'], 1)

    def test_an_unchanged_payload_costs_no_fetch(self):
        self.assertEqual(self.obs['afterRepeat'], 1)

    def test_a_healthy_card_starts_no_poll(self):
        self.assertEqual(self.obs['polls'], 0)


class StaleThenBuildingTests(_Base):
    """The sequence a real account sync produces, and the reason the trigger is not
    watching `ready`: the watermark moves (stale) before the rebuild starts (building),
    so the boolean is false at both steps and only the reason names which is which."""

    SCENARIO = 'staleThenBuilding'

    def test_both_steps_report_the_same_ready_flag(self):
        self.assertEqual(self.obs['readyFlags'], [False, False])

    def test_going_stale_re_reads_the_card_but_starts_no_poll(self):
        self.assertEqual(self.obs['afterStale']['calls'], self.obs['base'] + 1)
        self.assertEqual(self.obs['afterStale']['polls'], 0)
        self.assertIn('STALE', self.obs['afterStale']['text'])

    def test_the_rebuild_is_noticed_although_ready_did_not_move(self):
        self.assertEqual(self.obs['afterBuilding']['calls'], self.obs['base'] + 2)
        self.assertEqual(self.obs['afterBuilding']['polls'], 1)

    def test_the_card_names_the_rebuild_and_locks_the_button(self):
        self.assertIn('REBUILDING', self.obs['afterBuilding']['text'])
        self.assertEqual(self.obs['afterBuilding']['btn'],
                         {'disabled': True, 'label': 'Rebuilding...'})


class PollStopsWhereItAlwaysDidTests(_Base):
    SCENARIO = 'pollStops'

    def test_the_poll_that_started_is_the_cards_own_three_second_one(self):
        self.assertEqual(self.obs['running'], 1)

    def test_a_tick_that_finds_the_rebuild_done_stops_it(self):
        self.assertEqual(self.obs['stillRunning'], 0)
        self.assertEqual(self.obs['cleared'], [3000])

    def test_the_button_is_usable_again(self):
        self.assertEqual(self.obs['btn'], {'disabled': False, 'label': 'Rebuild now'})
        self.assertIn('READY', self.obs['text'])


class OneReaderAtATimeTests(_Base):
    SCENARIO = 'standsOffWhilePolling'

    def test_a_further_change_adds_no_second_reader(self):
        self.assertEqual(self.obs['polls'], 1)
        self.assertEqual(self.obs['after'], self.obs['whilePolling'])


class HookIsNotAWriterTests(_Base):
    SCENARIO = 'hookWritesNothing'

    def test_the_rows_change_only_when_the_loader_answers(self):
        self.assertTrue(self.obs['unchangedSynchronously'])
        self.assertTrue(self.obs['changedAfterFetch'])


class AbsentPayloadTests(_Base):
    SCENARIO = 'emptyPayload'

    def test_no_search_block_is_not_a_change(self):
        self.assertEqual(self.obs['after'], self.obs['base'])


class TriggeredFetchFailureTests(_Base):
    SCENARIO = 'triggeredFetchFails'

    def test_the_card_says_it_could_not_read_the_status(self):
        self.assertIn('Could not read index status', self.obs['text'])
        self.assertEqual(self.obs['polls'], 0)


if __name__ == '__main__':
    unittest.main()
