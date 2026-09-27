"""Tier 0 - the Recordings list keeps itself current, in a real DOM.

Guards dev/docs/BUGS.md 2026-09-26 @ 09:54:07 PM: /recordings was drawn once, so a finished
recording kept reading Recording with a "Stop recording" menu item, a scheduled one never
moved to In Progress, and "in 5 min" never counted down (dev/changelog/1144). Invariants:

  (a) The hook is registered, the page renders the signature the poll reports, and an
      agreeing payload fetches nothing.
  (b) A recording that starts moves from Scheduled to In Progress with its menu switching
      from Cancel to Stop, in one fetch with no reload, and the page converges.
  (c) A recording that finishes moves to Completed and offers Delete instead of Stop.
  (d) Search text, its filtering, the chosen sort and a filter chip all survive the swap.
  (e) An open row menu holds the swap; closing it lets the next poll through.
  (f) Once a minute in a visible tab the clock cells are rewritten from
      /api/recordings/times - in place, without re-rendering a row - and not at all in a
      background tab.
  (g) Deleting from a row menu refreshes the list instead of reloading the page.
  (h) Crossing the empty state reloads, in both directions - the empty page has no list
      to swap into.

tests/support/recordings_page.mjs replays each scenario against what the Flask app really
rendered at four database states; every assertion lives here.

  python3 -m unittest tests.test_recordings_page_live_js
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

from app import db  # noqa: E402
from app.database import (  # noqa: E402
    Recording, REC_STATUS_COMPLETED, REC_STATUS_IN_PROGRESS, REC_STATUS_SCHEDULED,
)
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'recordings_page.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None
_IDS = {}


def _observe():
    """Render every state once, drive every scenario once in node, return it all."""
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='recordings_page_js_')
    try:
        client = t.app.test_client()

        def snapshot(state):
            for suffix, url in (('html', '/recordings'), ('json', '/api/nav-status'),
                                ('times.json', '/api/recordings/times')):
                resp = client.get(url)
                if resp.status_code != 200:
                    raise AssertionError(f'{url} answered {resp.status_code} at state {state}')
                with open(os.path.join(tmp, f'{state}.{suffix}'), 'w', encoding='utf-8') as f:
                    f.write(resp.get_data(as_text=True))

        snapshot('empty')

        now = datetime.utcnow()
        soon = seed.make_recording(status=REC_STATUS_SCHEDULED, name='Zeta show',
                                   start_time=now + timedelta(minutes=10),
                                   stop_time=now + timedelta(minutes=70))
        live = seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='Alpha show',
                                   start_time=now - timedelta(minutes=20),
                                   stop_time=now + timedelta(minutes=40),
                                   started_at=now - timedelta(minutes=20))
        done = seed.make_recording(status=REC_STATUS_COMPLETED, name='Mid done',
                                   start_time=now - timedelta(hours=5),
                                   stop_time=now - timedelta(hours=4),
                                   completed_at=now - timedelta(hours=4))
        db.session.commit()
        _IDS.update(soon=soon.id, live=live.id, done=done.id)
        snapshot('scheduled')

        rec = db.session.get(Recording, _IDS['soon'])
        rec.status = REC_STATUS_IN_PROGRESS
        rec.started_at = datetime.utcnow()
        db.session.commit()
        snapshot('started')

        rec = db.session.get(Recording, _IDS['live'])
        rec.status = REC_STATUS_COMPLETED
        rec.completed_at = datetime.utcnow()
        db.session.commit()
        snapshot('finished')

        with open(os.path.join(tmp, 'ids.json'), 'w', encoding='utf-8') as f:
            json.dump(_IDS, f)

        proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=180, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}')
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


class BootTests(_Base):
    SCENARIO = 'boot'

    def test_the_hook_is_registered_and_the_signatures_agree(self):
        self.assertEqual(self.obs['hook'], 'function')
        self.assertTrue(self.obs['recSig'])
        self.assertEqual(self.obs['recSig'], self.obs['navSig'])

    def test_an_agreeing_payload_fetches_nothing(self):
        """Neither the page nor the times: a fresh render is not a minute old."""
        self.assertEqual(self.obs['pageFetches'], 0)
        self.assertEqual(self.obs['timesFetches'], 0)

    def test_the_rendered_starting_point(self):
        self.assertEqual(self.obs['soonSection'], 'sched')
        self.assertIn('cancel', self.obs['soonActs'])


class StartedTests(_Base):
    SCENARIO = 'started'

    def test_the_row_moves_to_in_progress_and_offers_stop(self):
        self.assertEqual(self.obs['after']['soonSection'], 'live')
        self.assertIn('stop', self.obs['after']['soonActs'])
        self.assertNotIn('cancel', self.obs['after']['soonActs'])

    def test_the_header_count_follows(self):
        self.assertNotEqual(self.obs['after']['sub'], self.obs['after']['subBefore'])
        self.assertIn('2 recording now', self.obs['after']['sub'])

    def test_one_fetch_no_reload_then_converges(self):
        self.assertEqual(self.obs['after']['pageFetches'], 1)
        self.assertEqual(self.obs['navigations'], [])
        self.assertEqual(self.obs['after']['recSig'], self.obs['after']['navSig'])
        self.assertEqual(self.obs['after']['pageFetchesAfterNextPoll'], 1)


class FinishedTests(_Base):
    SCENARIO = 'finished'

    def test_a_finished_recording_stops_offering_stop(self):
        """The defect as reported: it read Recording, with Stop recording in its menu."""
        self.assertEqual(self.obs['before']['section'], 'live')
        self.assertIn('stop', self.obs['before']['acts'])
        self.assertEqual(self.obs['after']['section'], 'done')
        self.assertIn('delete', self.obs['after']['acts'])
        self.assertNotIn('stop', self.obs['after']['acts'])
        self.assertEqual(self.obs['navigations'], [])


class SearchAndSortTests(_Base):
    SCENARIO = 'search_and_sort'

    def test_the_search_still_filters_the_new_rows(self):
        # 'show' matches Zeta show and Alpha show, not Mid done.
        self.assertEqual(sorted(self.obs['visibleBefore']), sorted([_IDS['soon'], _IDS['live']]))
        self.assertEqual(self.obs['search'], 'show')
        self.assertEqual(sorted(self.obs['visibleAfter']), sorted([_IDS['soon'], _IDS['live']]))

    def test_the_sort_survives(self):
        """Alpha before Zeta by name; the server renders In Progress newest-start first."""
        self.assertEqual(self.obs['sortedCol'], 'name:▴')
        self.assertEqual(self.obs['liveOrder'], [_IDS['live'], _IDS['soon']])


class FilterTests(_Base):
    SCENARIO = 'filter'

    def test_a_filter_chip_survives_and_filters_the_new_rows(self):
        self.assertEqual(self.obs['visibleBefore'], [_IDS['done']])
        self.assertEqual(len(self.obs['chipsBefore']), 1)
        self.assertEqual(self.obs['chipsAfter'], self.obs['chipsBefore'])
        self.assertEqual(sorted(self.obs['visibleAfter']), sorted([_IDS['live'], _IDS['done']]))


class MenuHoldTests(_Base):
    SCENARIO = 'menu_hold'

    def test_an_open_row_menu_holds_the_swap(self):
        """Replacing the row would close the menu mid-choice."""
        self.assertTrue(self.obs['menuOpen'])
        self.assertEqual(self.obs['whileOpen']['pageFetches'], 0)
        self.assertEqual(self.obs['whileOpen']['soonSection'], 'sched')
        self.assertTrue(self.obs['whileOpen']['stillOpen'])

    def test_closing_it_lets_the_next_poll_through(self):
        self.assertEqual(self.obs['afterClose']['pageFetches'], 1)
        self.assertEqual(self.obs['afterClose']['soonSection'], 'live')


class TickTests(_Base):
    SCENARIO = 'tick'

    def test_a_background_tab_is_not_ticked(self):
        self.assertEqual(self.obs['hiddenTab']['timesFetches'], 0)
        self.assertEqual(self.obs['hiddenTab']['rel'], self.obs['relBefore'])

    def test_a_visible_tab_rewrites_the_clock_cells_in_place(self):
        v = self.obs['visibleTab']
        self.assertEqual(v['timesFetches'], 1)
        self.assertEqual(v['rel'], 'TICK-REL')
        self.assertEqual(v['day'], 'TICK-DAY')
        self.assertTrue(v['sameRow'])
        self.assertEqual(v['pageFetches'], 0)

    def test_the_tick_restarts_the_minute(self):
        self.assertEqual(self.obs['timesFetchesRightAfter'], 1)


class ActionTests(_Base):
    SCENARIO = 'action'

    def test_a_delete_refreshes_the_list_instead_of_reloading(self):
        self.assertTrue(self.obs['hadConfirm'])
        self.assertIn(f'POST /recordings/{_IDS["done"]}/delete-json', self.obs['posts'])
        self.assertEqual(self.obs['pageFetches'], 1)
        self.assertEqual(self.obs['navigations'], [])


class EmptyBoundaryTests(unittest.TestCase):

    @unittest.skipIf(shutil.which('node') is None, 'node not installed')
    @unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
    def test_the_first_recording_reloads_the_empty_page(self):
        obs = _observe()['first_recording']
        self.assertEqual(obs.get('errors'), [], obs)
        self.assertEqual(obs['hook'], 'function')
        self.assertEqual(obs['after'] - obs['before'], 1)

    @unittest.skipIf(shutil.which('node') is None, 'node not installed')
    @unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
    def test_the_last_recording_going_reloads_the_list(self):
        obs = _observe()['last_recording']
        self.assertEqual(obs.get('errors'), [], obs)
        self.assertEqual(obs['pageFetches'], 1)
        self.assertEqual(obs['after'] - obs['before'], 1)


class TimesEndpointTests(unittest.TestCase):
    """The server half: the tick's words are the page's words."""

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def test_each_row_carries_the_same_day_and_relative_line_as_the_page(self):
        import re
        now = datetime.utcnow()
        with self.t.app.app_context():
            rid = seed.make_recording(status=REC_STATUS_SCHEDULED, name='Soon',
                                      start_time=now + timedelta(hours=2),
                                      stop_time=now + timedelta(hours=3)).id
            db.session.commit()
        d = self.client.get('/api/recordings/times').get_json()
        self.assertTrue(d['success'])
        t = d['rows'][str(rid)]
        page = self.client.get('/recordings').get_data(as_text=True)
        m = re.search(r'data-id="%d".*?<span class="day">(.*?)</span>.*?<span class="rel">(.*?)</span>'
                      % rid, page, re.S)
        self.assertIsNotNone(m)
        self.assertEqual((m.group(1), m.group(2)), (t['day'], t['rel']))
        self.assertTrue(t['rel'].startswith('in '))


if __name__ == '__main__':
    unittest.main()
