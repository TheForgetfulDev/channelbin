"""Tier 0 - the example channels' progress card on Get started, in a real DOM.

Starting the load used to reload the page, which lost the "Try it" box above and left the
browser scrolled to whatever had moved into its place, with nothing on screen saying the
load was running. The card is drawn in place instead, brought into view, and follows the
load by itself (dev/changelog/1396). Invariants:

  (a) Starting the load sends one request, reloads nothing, and draws the card running,
      scrolled to and focused; the steps under it move on (the "Try it" box goes).
  (b) Each poll moves the card with the load: the health check step runs once the sync
      is done, and every step is done at the end.
  (c) A step moving refreshes the steps under the card too; once the load is done the
      card stops polling.

tests/support/get_started_activity.mjs replays the scenario against the markup the routes
really rendered; every assertion lives here.

  python3 -m unittest tests.test_get_started_activity_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, examples, readiness, setup_steps  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.readiness import patched, result  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'get_started_activity.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
MACHINE_READY = {cid: result(readiness.READY, f'{cid} fine') for cid in setup_steps.MACHINE_CHECKS}

_RESULT = None


def _steps(done, running=None):
    steps = examples._new_steps()
    for s in done:
        steps[s] = {'state': examples.STEP_DONE, 'detail': f'{s} done.'}
    if running:
        steps[running]['state'] = examples.STEP_RUNNING
    return steps


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='get_started_activity_js_')
    try:
        client = t.app.test_client()

        def snapshot(state):
            db.session.remove()
            with patched(**MACHINE_READY):
                for name, url in (('page', '/get-started'), ('activity', '/get-started/activity')):
                    resp = client.get(url)
                    if resp.status_code != 200:
                        raise AssertionError(f'{url} answered {resp.status_code} at {state}')
                    with open(os.path.join(tmp, f'{name}-{state}.html'), 'w',
                              encoding='utf-8') as f:
                        f.write(resp.get_data(as_text=True))

        snapshot('offer')

        acc = seed.make_account(name=examples.ACCOUNT_NAME, is_example=True,
                                last_sync_at=datetime.utcnow())
        db.session.commit()
        started = datetime.utcnow() - timedelta(seconds=40)
        run = {'account_id': acc.id, 'started_at': started, 'refs': {}}
        with examples._lock:
            examples._loading.update(run, steps=_steps([examples.STEP_ACCOUNT],
                                                       examples.STEP_SYNC))
        snapshot('syncing')
        with examples._lock:
            examples._loading['steps'] = _steps(examples.STEPS[:-1], examples.STEP_TEST)
        snapshot('testing')
        with examples._lock:
            examples._loading.clear()
            examples._last.update(run, steps=_steps(examples.STEPS),
                                  finished_at=datetime.utcnow())
        snapshot('done')

        proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
        return _RESULT
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(shutil.which('node') and os.path.isdir(JSDOM), 'needs node + jsdom')
class ExamplesProgressCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = _observe()

    def test_a_page_with_no_load_polls_nothing(self):
        view = self.r['before']
        self.assertTrue(view['offer'])
        self.assertEqual(view['steps'], [])
        self.assertEqual(view['activityFetches'], 0)

    def test_starting_draws_the_card_in_place_and_brings_it_into_view(self):
        view = self.r['started']
        self.assertEqual(view['posts'], 1)
        self.assertEqual(view['navigations'], 0, 'the page reloaded instead')
        self.assertTrue(view['running'])
        self.assertIn('gs-activity', view['scrolled'])
        self.assertEqual(view['focused'], 'gs-activity')
        self.assertEqual(view['steps'][:2], ['account:done', 'sync:running'])
        self.assertFalse(view['offer'], 'the steps under the card did not move on')

    def test_the_card_follows_the_load(self):
        self.assertEqual(self.r['testing']['steps'][-2:], ['group:done', 'test:running'])
        # The polls swap the card's contents, never the focused element itself.
        self.assertEqual(self.r['testing']['focused'], 'gs-activity')
        self.assertEqual(self.r['done']['focused'], 'gs-activity')
        self.assertEqual({s.split(':')[1] for s in self.r['done']['steps']}, {'done'})
        self.assertFalse(self.r['done']['running'])

    def test_a_step_moving_refreshes_the_steps_and_a_finished_load_stops_polling(self):
        self.assertGreater(self.r['testing']['pageFetches'], self.r['started']['pageFetches'])
        self.assertGreater(self.r['done']['pageFetches'], self.r['testing']['pageFetches'])
        self.assertEqual(self.r['afterDone']['activityFetches'],
                         self.r['done']['activityFetches'])
        self.assertEqual(self.r['afterDone']['navigations'], 0)
        self.assertEqual(self.r['afterDone']['errors'], [])


if __name__ == '__main__':
    unittest.main()
