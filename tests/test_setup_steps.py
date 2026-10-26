"""The first-run setup steps and the dashboard banner that leads with them.

Built in dev/changelog/1279 from dev/mockups/44-first-run-setup.html (approved at round 3,
dev/changelog/1278). Every case is one of the mockup's install states, or one of the rules
the design rests on:

  * A step is done because the thing exists now, never because something was stored - so
    deleting the only account brings its step back by itself, while the recording it made
    still counts.
  * The banner leads with a step that needs the user before the next one in order, so a
    machine that cannot record is said even though the account step comes first.
  * The account step asks whether anything has synced, not whether every account is
    current: a stale sync on a working install is the Readiness check's to report, and
    must not bring the setup banner back.
  * The banner is server-rendered, has no dismiss, and its wrapper is always on the page so
    the dashboard's own swaps can bring it in and take it away.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_setup_steps
"""
import re
import unittest
from datetime import datetime, timedelta

from app import db, readiness, setup_steps
from app.database import (ACCOUNT_STATUS_ERROR, ACCOUNT_STATUS_SYNCING,
                          ACCOUNT_STATUS_UNSYNCED)
from tests.support.app import make_test_app
from tests.support.readiness import patched, result
from tests.support.seed import (make_account, make_channel, make_channel_test, make_group,
                                make_recording)

#: A test app has no scheduler and may have no ffmpeg; every machine check answers READY
#: unless a case says otherwise, so each case is about the step it names.
MACHINE_READY = {cid: result(readiness.READY, f'{cid} fine') for cid in setup_steps.MACHINE_CHECKS}


class _Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.test_request_context('/')
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def evaluate(self, **answers):
        with patched(**{**MACHINE_READY, **answers}):
            return setup_steps.evaluate()

    @staticmethod
    def states(setup):
        return [s['status'] for s in setup['steps']]

    @staticmethod
    def account(status=None, **kw):
        # make_account() always seeds OK; the account step reads the status, so set it after.
        acc = make_account(name='Northwind TV', **kw)
        if status is not None:
            acc.status = status
        return acc

    def synced_account(self, **kw):
        kw.setdefault('last_sync_at', datetime.utcnow() - timedelta(minutes=3))
        kw.setdefault('channel_count', 48)
        return self.account(**kw)


class StepStateTests(_Case):
    def test_a_brand_new_install_leads_with_adding_an_account(self):
        setup = self.evaluate()
        self.assertEqual(self.states(setup), [setup_steps.TODO, setup_steps.DONE,
                                              setup_steps.BLOCKED, setup_steps.BLOCKED])
        self.assertEqual(setup['lead']['id'], 'account')
        self.assertEqual(setup['lead_word'], 'Next')
        self.assertEqual(setup['lead']['link']['url'], '/accounts')
        self.assertEqual((setup['done'], setup['total']), (1, 4))
        self.assertFalse(setup['finished'])

    def test_a_machine_that_cannot_record_outranks_the_next_step(self):
        """The mockup's "brand new, and ffmpeg is missing": the problem leads, not the
        account step that comes first in order."""
        setup = self.evaluate(ffmpeg=result(readiness.PROBLEM, 'It could not be run at ffmpeg'))
        self.assertEqual(setup['lead']['id'], 'machine')
        self.assertEqual(setup['lead_word'], 'Needs you')
        self.assertIn('It could not be run at ffmpeg', setup['lead']['short'])
        self.assertEqual(setup['lead']['link']['label'], 'Settings')
        self.assertEqual(setup['done'], 0)

    def test_a_machine_check_that_could_not_answer_is_not_a_pass(self):
        setup = self.evaluate(storage_space=result(readiness.UNKNOWN, 'no honest answer'))
        machine = setup['steps'][1]
        self.assertEqual(machine['status'], setup_steps.PROBLEM)
        self.assertIn('Could not check', machine['state'])

    def test_a_silenced_machine_check_does_not_hold_the_step_back(self):
        readiness.set_check_ignored('storage_space', True)
        setup = self.evaluate(storage_space=result(readiness.PROBLEM, '99% used'))
        self.assertEqual(setup['steps'][1]['status'], setup_steps.DONE)

    def test_low_disk_space_still_lets_the_machine_record(self):
        setup = self.evaluate(storage_space=result(readiness.ATTENTION, '88% used'))
        self.assertEqual(setup['steps'][1]['status'], setup_steps.DONE)

    def test_an_account_waiting_for_its_first_sync_is_in_progress(self):
        acc = self.account(status=ACCOUNT_STATUS_UNSYNCED,
                           next_sync_at=datetime.utcnow() + timedelta(minutes=4, seconds=30))
        db.session.commit()
        setup = self.evaluate()
        self.assertEqual(setup['lead']['status'], setup_steps.WAITING)
        self.assertEqual(setup['lead_word'], 'In progress')
        self.assertIn("Northwind TV's first sync starts in 4m", setup['lead']['short'])
        self.assertEqual(setup['lead']['link']['url'], f'/accounts/{acc.id}')

    def test_an_account_whose_scheduled_sync_is_off_says_so(self):
        self.account(status=ACCOUNT_STATUS_UNSYNCED, sync_enabled=False)
        db.session.commit()
        lead = self.evaluate()['lead']
        self.assertEqual(lead['status'], setup_steps.WAITING)
        self.assertIn('scheduled sync is off', lead['short'])

    def test_an_account_mid_first_sync_is_in_progress(self):
        self.account(status=ACCOUNT_STATUS_SYNCING)
        db.session.commit()
        lead = self.evaluate()['lead']
        self.assertEqual(lead['status'], setup_steps.WAITING)
        self.assertIn('running now', lead['short'])

    def test_a_failed_first_sync_needs_the_user_and_says_why(self):
        self.account(status=ACCOUNT_STATUS_ERROR,
                     last_error='HTTP 401 Unauthorized\nTraceback follows')
        db.session.commit()
        lead = self.evaluate()['lead']
        self.assertEqual(lead['status'], setup_steps.PROBLEM)
        self.assertEqual(lead['short'], "Northwind TV's first sync failed: HTTP 401 Unauthorized")

    def test_a_synced_account_with_nothing_in_the_guide_leads_with_the_guide(self):
        self.synced_account()
        db.session.commit()
        setup = self.evaluate()
        self.assertEqual(self.states(setup), [setup_steps.DONE, setup_steps.DONE,
                                              setup_steps.TODO, setup_steps.BLOCKED])
        self.assertEqual(setup['lead']['id'], 'guide')
        self.assertIn('48 channels came in', setup['lead']['short'])
        self.assertEqual(setup['lead']['link']['label'], 'Channel Search')

    def test_a_channel_in_the_guide_leads_with_recording_something(self):
        acc = self.synced_account()
        make_channel(acc, name='Northwind News HD', in_guide=True)
        db.session.commit()
        setup = self.evaluate()
        self.assertEqual(setup['lead']['id'], 'record')
        self.assertEqual(setup['lead']['link']['label'], 'TV Guide')
        self.assertEqual(setup['done'], 3)

    def test_a_scheduled_recording_finishes_setup(self):
        acc = self.synced_account()
        ch = make_channel(acc, name='Northwind News HD', in_guide=True)
        make_recording(status='SCHEDULED', channel_id=ch.id)
        db.session.commit()
        setup = self.evaluate()
        self.assertTrue(setup['finished'])
        self.assertIsNone(setup['lead'])
        self.assertEqual(setup['done'], 4)

    def test_deleting_the_only_account_brings_its_steps_back_and_keeps_the_recording(self):
        """Nothing was stored as done, so nothing has to be un-done: the steps read what is
        there. The recording is history, not setup, and still counts."""
        make_recording(status='COMPLETED', channel_id=None)
        db.session.commit()
        setup = self.evaluate()
        self.assertEqual(self.states(setup), [setup_steps.TODO, setup_steps.DONE,
                                              setup_steps.BLOCKED, setup_steps.DONE])
        self.assertEqual(setup['lead']['id'], 'account')
        self.assertEqual(setup['done'], 2)

    def test_a_failing_sync_on_a_working_install_does_not_bring_the_banner_back(self):
        """account_sync is red here; the setup question is whether anything has synced."""
        acc = self.synced_account(status=ACCOUNT_STATUS_ERROR, last_error='timed out')
        ch = make_channel(acc, name='Northwind News HD', in_guide=True)
        make_recording(status='COMPLETED', channel_id=ch.id)
        db.session.commit()
        with patched(**MACHINE_READY):
            sync = next(r for r in readiness.evaluate()['checks'] if r['id'] == 'account_sync')
        self.assertEqual(sync['status'], readiness.PROBLEM)
        self.assertTrue(self.evaluate()['finished'])

    def test_every_state_the_banner_can_lead_with_has_a_word(self):
        self.assertEqual(set(setup_steps.LEAD_WORD),
                         {setup_steps.PROBLEM, setup_steps.WAITING, setup_steps.TODO})
        self.assertEqual(set(setup_steps.STEP_STATES) - set(setup_steps.LEAD_WORD),
                         {setup_steps.DONE, setup_steps.BLOCKED})


class DashboardBannerTests(_Case):
    def get_dashboard(self, **answers):
        with patched(**{**MACHINE_READY, **answers}):
            return self.t.app.test_client().get('/').get_data(as_text=True)

    def test_a_new_install_gets_the_banner_above_the_tiles(self):
        html = self.get_dashboard()
        self.assertIn('class="gs-banner"', html)
        self.assertIn('Next: Add your IPTV account(s)', html)
        self.assertIn('Get started &middot; 1 of 4 done', html)
        self.assertLess(html.index('class="gs-banner"'), html.index('class="dash-pulse"'))

    def test_the_bar_fills_from_the_left_by_the_done_count(self):
        """Round 3's fix (dev/changelog/1278): a new install's machine step is done, and a
        per-step fill lit the second segment under a banner naming step 1."""
        html = self.get_dashboard()
        segs = re.findall(r'<i class="gs-seg( done)?"></i>', html)
        self.assertEqual(segs, [' done', '', '', ''])

    def test_the_banner_is_a_jump_off_with_no_dismiss(self):
        html = self.get_dashboard()
        banner = html[html.index('<div id="dash-setup">'):html.index('class="dash-pulse"')]
        self.assertNotIn('<button', banner)
        link = re.search(r'<a class="btn gs-b-second" href="([^"]+)">Accounts <span class="arw">', banner)
        self.assertIsNotNone(link, banner)
        self.assertEqual(link.group(1), '/accounts')

    def test_get_started_is_the_banners_first_button(self):
        """Mockup 44 round 3: Get started is the primary, the lead step's page the second."""
        html = self.get_dashboard()
        banner = html[html.index('<div id="dash-setup">'):html.index('class="dash-pulse"')]
        hrefs = re.findall(r'<a class="btn (gs-b-\w+)" href="([^"]+)">', banner)
        self.assertEqual(hrefs, [('gs-b-go', '/get-started'), ('gs-b-second', '/accounts')])

    def test_a_readiness_context_that_cannot_be_built_costs_the_banner_not_the_page(self):
        """A scheduler stand-in with no `running` made Context raise, and the whole
        dashboard answered 500 (found by tests/test_jobs_schedule_truth.py)."""
        from unittest import mock
        with mock.patch.object(readiness, 'Context', side_effect=AttributeError('running')), \
                self.assertLogs('app.routes.dashboard', level='WARNING') as logs:
            resp = self.t.app.test_client().get('/')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('<div id="dash-setup">', html)
        self.assertNotIn('gs-banner', html)
        self.assertIn('banner left out', '\n'.join(logs.output))

    def test_a_set_up_install_keeps_the_empty_wrapper_and_no_banner(self):
        acc = self.synced_account()
        ch = make_channel(acc, name='Northwind News HD', in_guide=True)
        make_recording(status='COMPLETED', channel_id=ch.id)
        db.session.commit()
        html = self.get_dashboard()
        self.assertIn('<div id="dash-setup">', html)
        self.assertNotIn('gs-banner', html)


class OptionalStepTests(_Case):
    """The Get started page's optional steps (dev/changelog/1280): each read from what exists,
    never the accented step, and never counted in "N of 4"."""

    def page(self, **answers):
        with patched(**{**MACHINE_READY, **answers}):
            return setup_steps.page()

    @staticmethod
    def by_id(setup):
        return {s['id']: s for s in setup['steps'] + setup['optional']}

    def test_a_new_install_waits_on_an_account_for_everything_but_notifications(self):
        steps = self.by_id(self.page())
        for sid in ('test', 'group', 'rule'):
            self.assertEqual(steps[sid]['status'], setup_steps.BLOCKED, sid)
        self.assertEqual(steps['notify']['status'], setup_steps.TODO)

    def test_after_a_sync_each_optional_step_teaches_where_its_control_is(self):
        self.synced_account()
        db.session.commit()
        steps = self.by_id(self.page())
        self.assertIn('Test now', steps['test']['body'])
        self.assertIn('+ New Group', steps['group']['body'])
        self.assertIn('Create rule from this search', steps['rule']['body'])
        self.assertEqual(steps['group']['link']['url'], '/channel-groups')

    def test_a_health_check_result_names_its_channel(self):
        acc = self.synced_account()
        ch = make_channel(acc, name='Classic Movies East')
        make_channel_test(ch)
        db.session.commit()
        step = self.by_id(self.page())['test']
        self.assertEqual(step['status'], setup_steps.DONE)
        self.assertIn('Classic Movies East', step['state'])

    def test_only_a_group_of_your_own_counts(self):
        """The automatic TV Guide Channels group is the app's, not the user's."""
        acc = self.synced_account()
        sysgrp = make_group(name='TV Guide Channels')
        sysgrp.is_system = True
        db.session.commit()
        self.assertEqual(self.by_id(self.page())['group']['status'], setup_steps.TODO)
        make_group(name='News feeds', members=[make_channel(acc, name='News HD')])
        db.session.commit()
        step = self.by_id(self.page())['group']
        self.assertEqual(step['status'], setup_steps.DONE)
        self.assertIn('1 channel group', step['state'])

    def test_an_example_group_is_not_a_group_of_your_own(self):
        """BUGS.md 2026-10-26: "1 channel group of your own" for the example Bloomberg group."""
        acc = self.synced_account()
        make_group(name='Bloomberg TV (example)', is_example=True,
                   members=[make_channel(acc, name='Bloomberg TV Europe')])
        db.session.commit()
        step = self.by_id(self.page())['group']
        self.assertEqual(step['status'], setup_steps.TODO)
        self.assertIn('No channel group of your own yet', step['state'])
        self.assertIn('The example group "Bloomberg TV (example)" shows what one is', step['state'])

    def test_a_check_on_an_example_channel_is_named_but_does_not_finish_the_step(self):
        """The examples run a health check of their own (dev/changelog/1288)."""
        ex = make_account(name='Example channels (M3UPT)', is_example=True,
                          last_sync_at=datetime.utcnow())
        make_channel_test(make_channel(ex, name='BBC News'))
        db.session.commit()
        step = self.by_id(self.page())['test']
        self.assertEqual(step['status'], setup_steps.TODO)
        self.assertIn('Only an example channel has been checked so far: BBC News', step['state'])
        mine = make_account(name='Northwind TV', last_sync_at=datetime.utcnow())
        make_channel_test(make_channel(mine, name='Classic Movies East'))
        db.session.commit()
        step = self.by_id(self.page())['test']
        self.assertEqual(step['status'], setup_steps.DONE)
        self.assertIn('Classic Movies East', step['state'])

    def test_a_service_that_is_on_finishes_the_notify_step(self):
        from types import SimpleNamespace
        step = setup_steps._notify_step(SimpleNamespace(notify_enabled=['pushover', 'discord']))
        self.assertEqual(step['status'], setup_steps.DONE)
        self.assertEqual(step['state'], 'Pushover and Discord are on.')

    def test_optional_steps_never_count_toward_the_four(self):
        setup = self.page()
        self.assertEqual(setup['total'], 4)
        self.assertTrue(all(s['optional'] for s in setup['optional']))
        self.assertEqual([s['n'] for s in setup['optional']], [5, 6, 7, 8])

    def test_one_core_step_is_accented_and_never_an_optional_one(self):
        self.synced_account()
        db.session.commit()
        setup = self.page()
        looks = [s['display'] for s in setup['steps']]
        self.assertEqual(looks, ['done', 'done', 'now', 'later'])
        self.assertNotIn('now', [s['display'] for s in setup['optional']])

    def test_no_step_is_accented_while_a_core_step_needs_the_user(self):
        """A problem carries its own color; a second accented step would be a second
        answer to "what now"."""
        setup = self.page(ffmpeg=result(readiness.PROBLEM, 'missing'))
        looks = [s['display'] for s in setup['steps']]
        self.assertEqual(looks, ['todo', 'problem', 'later', 'later'])

    def test_the_finish_card_names_what_the_maintenance_numbers_count(self):
        readiness_part = self.page()['readiness']
        with patched(**MACHINE_READY):
            nav = readiness.evaluate()['nav']
        self.assertEqual(len(readiness_part['blocked']), nav['blocked'])
        self.assertEqual(len(readiness_part['degraded']), nav['degraded'])
        self.assertIn('See what is on', readiness_part['blocked'] + readiness_part['degraded'])
        self.assertTrue(readiness_part['clears_some'])


class GetStartedPageTests(_Case):
    def get(self, path='/get-started', **answers):
        with patched(**{**MACHINE_READY, **answers}):
            return self.t.app.test_client().get(path).get_data(as_text=True)

    def test_the_page_lists_the_steps_in_order_then_search_then_readiness(self):
        html = self.get()
        order = [html.index(f'data-step="{sid}"') for sid in
                 ('account', 'machine', 'guide', 'record', 'test', 'group', 'rule', 'notify')]
        self.assertEqual(order, sorted(order))
        self.assertLess(order[-1], html.index('id="gs-search"'))
        self.assertLess(html.index('id="gs-search"'), html.index('id="gs-finish"'))
        self.assertIn('1 of 4 steps done', html)

    def test_the_search_card_tells_the_two_searches_apart_and_opens_each(self):
        html = self.get()
        card = html[html.index('id="gs-search"'):html.index('id="gs-finish"')]
        self.assertIn('<h3>Search Channels</h3>', card)
        self.assertIn('<h3>Search Programs (EPG)</h3>', card)
        links = dict((label, href) for href, label in
                     re.findall(r'<a class="btn btn-sm" href="([^"]+)">([^<]+) <span', card))
        self.assertEqual(links['Search Channels'], '/channels')
        self.assertIn('grain=airings', links['Search Programs (EPG)'])

    def test_the_finish_card_quotes_the_blocked_capabilities(self):
        html = self.get()
        finish = html[html.index('id="gs-finish"'):]
        self.assertIn('"See what is on"', finish)
        self.assertIn('nav-count-ready-', finish)

    def test_a_finished_install_says_so_and_keeps_the_steps(self):
        acc = self.synced_account()
        ch = make_channel(acc, name='Northwind News HD', in_guide=True)
        make_recording(status='COMPLETED', channel_id=ch.id)
        db.session.commit()
        html = self.get()
        self.assertIn('all 4 steps done', html)
        self.assertIn('ChannelBin is set up.', html)
        self.assertIn('data-step="record"', html)

    def test_a_waiting_step_asks_for_the_minute_tick(self):
        self.account(status=ACCOUNT_STATUS_UNSYNCED,
                     next_sync_at=datetime.utcnow() + timedelta(minutes=4))
        db.session.commit()
        self.assertIn('data-waiting="1"', self.get())
        db.session.query(type(self.account())).delete()
        db.session.commit()
        self.assertNotIn('data-waiting', self.get())

    def test_a_readiness_context_that_cannot_be_built_says_so_on_the_page(self):
        from unittest import mock
        with mock.patch.object(readiness, 'Context', side_effect=AttributeError('running')), \
                self.assertLogs('app.routes.setup', level='WARNING') as logs:
            resp = self.t.app.test_client().get('/get-started')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('could not be read just now', html)
        self.assertIn('id="gs-search"', html)
        self.assertIn('could not be read', '\n'.join(logs.output))

    def test_the_readiness_card_links_back_here(self):
        html = self.get('/maintenance')
        card = html[html.index('id="m-readiness"'):html.index('id="m-storage"')]
        self.assertIn('href="/get-started">Get started <span class="arw">', card)


class MenuLinkTests(_Case):
    """The "Get started" menu link: first under Setup, an "N left" count from the cached
    reading, and not drawn once the four are done (dev/changelog/1280)."""

    def nav_status(self, **answers):
        with patched(**{**MACHINE_READY, **answers}):
            return self.t.app.test_client().get('/api/nav-status').get_json()

    def link(self, path='/tags'):
        html = self.t.app.test_client().get(path).get_data(as_text=True)
        m = re.search(r'<a class="nav-link nav-gs[^"]*" href="/get-started"([^>]*)>.*?'
                      r'<span class="nav-count nav-count-setup">([^<]*)</span>', html)
        self.assertIsNotNone(m, 'no Get started link')
        return 'display:none' not in m.group(1), m.group(2)

    def test_the_poll_carries_the_count(self):
        self.assertEqual(self.nav_status()['setup'], {'left': 3, 'total': 4})

    def test_with_nothing_cached_the_link_starts_hidden(self):
        self.assertEqual(self.link()[0], False)

    def test_after_a_poll_the_link_is_drawn_with_its_count(self):
        self.nav_status()
        self.assertEqual(self.link(), (True, '3 left'))

    def test_the_link_has_no_tint_of_its_own(self):
        """Its tint matched the current page's, so two menu items looked selected
        (dev/changelog/1288). The tint is .active's alone."""
        import os
        css_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'static', 'css', 'style.css')
        with open(css_path, encoding='utf-8') as f:
            css = f.read()
        rules = re.findall(r'([^{}]*\.nav-gs[^{}]*)\{([^}]*)\}', css)
        self.assertTrue(rules)
        for selector, body in rules:
            self.assertNotIn('background', body, selector.strip())

    def test_the_link_sits_first_under_setup(self):
        self.nav_status()
        html = self.t.app.test_client().get('/tags').get_data(as_text=True)
        setup = html.index('<span class="nav-t">Setup</span>')
        self.assertLess(setup, html.index('nav-gs'))
        self.assertLess(html.index('nav-gs'), html.index('href="/accounts"', setup))

    def test_a_new_recording_throws_the_cached_count_away(self):
        """A first recording scheduled from another page must not leave "1 left" in the
        menu for the cache's whole lifetime."""
        acc = self.synced_account()
        ch = make_channel(acc, name='Northwind News HD', in_guide=True)
        db.session.commit()
        self.assertEqual(self.nav_status()['setup']['left'], 1)
        self.assertEqual(self.link(), (True, '1 left'))
        make_recording(status='SCHEDULED', channel_id=ch.id)
        db.session.commit()
        self.assertEqual(self.link()[0], False)
        self.assertEqual(self.nav_status()['setup']['left'], 0)

    def test_a_new_guide_channel_throws_the_cached_count_away(self):
        acc = self.synced_account()
        db.session.commit()
        self.assertEqual(self.nav_status()['setup']['left'], 2)
        make_channel(acc, name='Northwind News HD', in_guide=True)
        db.session.commit()
        self.assertIsNone(setup_steps.cached_nav(setup_steps.state_key()))
        self.assertEqual(self.nav_status()['setup']['left'], 1)

    def test_a_set_up_install_draws_no_link(self):
        acc = self.synced_account()
        ch = make_channel(acc, name='Northwind News HD', in_guide=True)
        make_recording(status='COMPLETED', channel_id=ch.id)
        db.session.commit()
        self.nav_status()
        self.assertEqual(self.link()[0], False)

    def test_the_poll_survives_steps_that_cannot_be_read(self):
        from unittest import mock
        with mock.patch.object(readiness, 'Context', side_effect=AttributeError('running')), \
                self.assertLogs('app.setup_steps', level='WARNING'):
            resp = self.t.app.test_client().get('/api/nav-status')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['setup'], {'left': 0, 'total': 0})


if __name__ == '__main__':
    unittest.main()
