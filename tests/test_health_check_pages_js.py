"""Tier 0 - the Groups list and the Channel page following a health check run, driven in a
real DOM (dev/changelog/1158).

Both pages were load-once for test results: /channel-groups showed a check as Running long
after it finished, and kept every member's verdict and the best-member star from load time;
/channels/<id> caught up only after its own Test now, by polling the tester and reloading,
so a group's check or the nightly one left "Testing now", the score and the test history
stale. Both now swap their result regions when base.html's /api/nav-status poll reports a
health-check signature they were not rendered at - and all of that happens in the browser,
so none of it is visible from a response body.

tests/support/health_check_pages.mjs runs the shipped scripts against the pages and
nav-status payloads the Flask app really answered at three states (idle, a run testing the
channel, that run finished) and reports; every assertion lives here.

What it cannot cover: jsdom computes no layout. The signature itself is
tests/test_health_check_signature.py.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_health_check_pages_js
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

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import channel_tester, db  # noqa: E402
from app.database import (  # noqa: E402
    Channel, ChannelTest, OnDemandTestJob, OD_JOB_STATUS_COMPLETED, OD_JOB_STATUS_RUNNING,
    TEST_STATUS_COMPLETED,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'health_check_pages.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_BREAKDOWN = json.dumps({'base': 100, 'penalties': [], 'final': 90})
_RESULT = None


def _observe():
    """Render the three states once, drive every scenario once in node, return it all."""
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='health_check_pages_js_')
    try:
        client = t.app.test_client()
        with t.app.app_context():
            now = datetime.utcnow()
            acct = seed.make_account(name='Acct')
            alpha = seed.make_channel(acct, name='Alpha Feed', health_score=90)
            beta = seed.make_channel(acct, name='Beta Feed')
            gamma = seed.make_channel(acct, name='Gamma')
            pair = seed.make_group(name='Pair', members=[alpha, beta], in_guide=True)
            seed.make_group(name='Other', members=[gamma], in_guide=False)
            seed.make_channel_test(alpha, all_null=False, status=TEST_STATUS_COMPLETED,
                                   test_started_at=now - timedelta(hours=2),
                                   test_ended_at=now - timedelta(hours=2),
                                   quality_score=90, lifetime_score_after=90,
                                   quality_breakdown=_BREAKDOWN)
            db.session.commit()
            alpha_id, job_id = alpha.id, pair.check.id
            channel_path = f'/channels/{alpha_id}'

        def snapshot(state):
            for name, url in (('groups', '/channel-groups'), ('channel', channel_path),
                              ('nav', '/api/nav-status')):
                resp = client.get(url)
                if resp.status_code != 200:
                    raise AssertionError(f'{url} answered {resp.status_code} at state {state}')
                ext = 'json' if name == 'nav' else 'html'
                with open(os.path.join(tmp, f'{name}-{state}.{ext}'), 'w', encoding='utf-8') as f:
                    f.write(resp.get_data(as_text=True))

        snapshot('idle')

        with t.app.app_context():
            with channel_tester._lock:
                channel_tester._reset_run_state(job_id=job_id, label='test run')
                channel_tester._state.current_channel_id = alpha_id
            db.session.get(OnDemandTestJob, job_id).status = OD_JOB_STATUS_RUNNING
            running = seed.make_channel_test(db.session.get(Channel, alpha_id),
                                             test_started_at=datetime.utcnow(),
                                             test_ended_at=None, job_id=job_id)
            db.session.commit()
            test_id = running.id
        snapshot('running')

        with t.app.app_context():
            test = db.session.get(ChannelTest, test_id)
            test.status = TEST_STATUS_COMPLETED
            test.test_ended_at = datetime.utcnow()
            test.connected = True
            test.quality_score = 80
            test.lifetime_score_after = 85
            test.quality_breakdown = _BREAKDOWN
            db.session.get(Channel, alpha_id).health_score = 85
            db.session.get(OnDemandTestJob, job_id).status = OD_JOB_STATUS_COMPLETED
            db.session.commit()
            channel_tester._end_run()
        snapshot('done')

        with open(os.path.join(tmp, 'meta.json'), 'w', encoding='utf-8') as f:
            json.dump({'channelPath': channel_path}, f)
        proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
        return _RESULT
    finally:
        channel_tester._end_run()
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


# ── Groups list ──────────────────────────────────────────────────────────

class GroupsListAsItOpensTests(_Base):
    SCENARIO = 'groups_boot'

    def test_the_list_registers_its_hook_on_the_nav_status_poll(self):
        self.assertEqual(self.obs['hook'], 'function')

    def test_the_list_was_rendered_at_the_signature_the_poll_reports(self):
        self.assertTrue(self.obs['navSig'])
        self.assertEqual(self.obs['listSig'], self.obs['navSig'])

    def test_an_unchanged_signature_fetches_nothing(self):
        """base.html's own first poll has run by now."""
        self.assertEqual(self.obs['pageFetches'], 0)
        self.assertEqual(self.obs['running'], 1)


class GroupsListRunFinishesTests(_Base):
    """The reported defect: a finished check kept reading Running, and the members kept
    their load-time verdicts."""
    SCENARIO = 'groups_finished'

    def test_the_list_is_swapped_once(self):
        self.assertEqual(self.obs['pageFetches'], 1)
        self.assertTrue(self.obs['swapped'])
        self.assertEqual(self.obs['listSig'], self.obs['navSig'])

    def test_the_next_poll_fetches_nothing_more(self):
        self.assertEqual(self.obs['pageFetchesAfterNextPoll'], 1)

    def test_running_goes_and_the_new_verdict_lands(self):
        self.assertEqual(self.obs['before']['running'], 1)
        self.assertEqual(self.obs['running'], 0)
        self.assertIn('PASS', self.obs['statuses'])

    def test_an_open_group_stays_open(self):
        self.assertTrue(self.obs['expanded'])

    def test_the_member_sort_is_put_back(self):
        self.assertEqual(self.obs['order'], self.obs['before']['order'])
        self.assertEqual(self.obs['order'], ['beta feed', 'alpha feed'])
        self.assertTrue(self.obs['nameThSorted'])

    def test_search_filter_and_section_sort_are_put_back(self):
        self.assertTrue(self.obs['otherHidden'], 'the swapped-in row escaped the filter')
        self.assertTrue(self.obs['pairShown'])
        self.assertEqual(self.obs['count'], self.obs['before']['count'])
        self.assertEqual(self.obs['chips'], 1)
        self.assertEqual(self.obs['search'], 'feed')
        self.assertEqual(self.obs['sortChip'], self.obs['before']['sortChip'])

    def test_the_swapped_in_rows_still_respond(self):
        self.assertTrue(self.obs['collapsesAfterSwap'])


class GroupsListMenuOpenTests(_Base):
    """Swapping a row out from under an open kebab would close it mid-choice."""
    SCENARIO = 'groups_menu_open'

    def test_an_open_menu_holds_the_swap_until_it_closes(self):
        self.assertTrue(self.obs['menuOpen'])
        self.assertEqual(self.obs['whileOpen'], {'pageFetches': 0, 'running': 1})
        self.assertEqual(self.obs['afterClose'], {'pageFetches': 1, 'running': 0})


# ── Channel page ─────────────────────────────────────────────────────────

class ChannelPageAsItOpensTests(_Base):
    SCENARIO = 'channel_boot'

    def test_the_page_registers_its_hook_on_the_nav_status_poll(self):
        self.assertEqual(self.obs['hook'], 'function')

    def test_the_page_was_rendered_at_the_signature_the_poll_reports(self):
        self.assertEqual(self.obs['barSig'], self.obs['navSig'])

    def test_an_unchanged_signature_fetches_nothing(self):
        self.assertEqual(self.obs['pageFetches'], 0)
        self.assertEqual(self.obs['bar'], 'Testing now')

    def test_tester_busy_comes_from_the_poll(self):
        self.assertIs(self.obs['testerBusy'], True)


class ChannelPageRunFinishesTests(_Base):
    """The reported defect: a check started elsewhere left "Testing now", the score and the
    history in place until a reload."""
    SCENARIO = 'channel_finished'

    def test_the_regions_are_swapped_once(self):
        self.assertEqual(self.obs['pageFetches'], 1)
        self.assertTrue(self.obs['swapped'])
        self.assertEqual(self.obs['barSig'], self.obs['navSig'])
        self.assertEqual(self.obs['pageFetchesAfterNextPoll'], 1)

    def test_the_status_bar_and_history_show_the_result(self):
        self.assertEqual(self.obs['before']['bar'], 'Testing now')
        self.assertEqual(self.obs['bar'], 'Healthy')
        self.assertEqual(self.obs['rows'], 2)
        self.assertEqual(self.obs['inProgress'], 0)

    def test_whats_on_is_left_to_guide_js(self):
        self.assertTrue(self.obs['whatsonKept'])

    def test_an_open_score_breakdown_stays_open(self):
        self.assertIn(self.obs['before']['openKey'], self.obs['openKeys'])

    def test_tester_busy_follows_the_poll(self):
        self.assertIs(self.obs['testerBusy'], False)

    def test_the_rollback_confirm_quotes_the_swapped_state(self):
        """Two scored tests now, so one step back leaves one more available. A confirm read
        from the load-time state would say none."""
        self.assertEqual(self.obs['before']['available'], 1)
        self.assertEqual(self.obs['available'], 2)
        self.assertIn('1 further step-back would', self.obs['stepBackBody'])

    def test_the_sticky_test_button_is_given_back(self):
        self.assertTrue(self.obs['stickyEnabled'])


class ChannelPageTestNowTests(_Base):
    """Test now no longer runs a reload poll of its own - the nav poll is the page's one
    updater for a result."""
    SCENARIO = 'channel_test_now'

    def test_it_starts_the_test_and_nothing_else(self):
        self.assertEqual(len(self.obs['posts']), 1)
        self.assertTrue(self.obs['posts'][0].endswith('/test-now'))
        self.assertEqual(self.obs['statusPolls'], 0)
        self.assertEqual(self.obs['navigations'], 0)
        self.assertTrue(self.obs['disabled'])


class ChannelPageScreenshotTests(_Base):
    """Capture screenshot posts once, lands in the Health card through the same swap a
    test result uses, and opens the frame (dev/changelog/1160)."""
    SCENARIO = 'channel_screenshot'

    def test_the_page_ran_without_errors(self):
        self.assertEqual(self.obs['errors'], [])

    def test_the_sticky_test_button_is_given_back(self):
        self.skipTest('not this scenario')

    def test_it_is_offered_in_the_bar_and_the_kebab(self):
        self.assertTrue(self.obs['inKebab'])

    def test_it_posts_once_and_refreshes_through_the_swap(self):
        self.assertEqual(len(self.obs['posts']), 1)
        self.assertTrue(self.obs['posts'][0].endswith('/screenshot'))
        self.assertEqual(self.obs['pageFetches'], 1)
        self.assertEqual(self.obs['navigations'], 0)

    def test_the_frame_opens_and_the_buttons_come_back(self):
        self.assertTrue(self.obs['disabledWhileRunning'])
        self.assertTrue(self.obs['lightboxShown'])
        self.assertIn('manual-ch1.jpg', self.obs['lightboxSrc'])
        self.assertTrue(self.obs['enabledAfter'])


class ChannelPageMenuOpenTests(_Base):
    SCENARIO = 'channel_menu_open'

    def test_an_open_menu_holds_the_swap_until_it_closes(self):
        self.assertTrue(self.obs['menuOpen'])
        self.assertEqual(self.obs['whileOpen'], {'pageFetches': 0, 'bar': 'Testing now'})
        self.assertEqual(self.obs['afterClose'], {'pageFetches': 1, 'bar': 'Healthy'})


if __name__ == '__main__':
    unittest.main()
