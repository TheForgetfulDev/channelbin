"""Example channels: "Try it with example channels" and Delete the examples (app/examples.py,
dev/changelog/1281).

The rules the feature rests on, one class each:

  * What is an example is recorded when it is created - `Account.is_example` and
    `ChannelGroup.is_example` - and never inferred from a name or a URL.
  * The load does what the Get started page says it does: the account reads M3UPT's public
    playlist and listings, the named channels get guide rows through the one Add to Guide
    write, the Bloomberg pair becomes an example group, one channel is health checked. Any
    part that does not happen is named in one EXAMPLES_INCOMPLETE alert.
  * Deleting the examples takes out exactly the example account and what hangs off it, and
    nothing of the user's: their own account, their group (minus the example channels),
    recordings made from an example channel. An example group the user added a channel to
    is theirs by then and is kept, with its timeline saying why.
  * A recording still under way or scheduled on an example channel refuses the delete and is
    named, rather than being cancelled for the user.
  * What the page promises is what the delete does: everything on an example channel goes,
    a guide row or a health check the user added there included (dev/changelog/1288).
  * The example account is a trial, never the user's account: it opens Get started's later
    steps but does not finish the account step, until the user adds their own or keeps the
    examples, which clears both markers and makes them ordinary (dev/changelog/1288).

Runs against a throwaway temp SQLite DB - never the live dvr.db, never the network: the sync
and the health check are stubbed.
  python3 -m unittest tests.test_examples
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from app import db, examples, readiness, setup_steps
from app import migrations as M
from app.database import (ACCOUNT_STATUS_ERROR, ACCOUNT_STATUS_OK, ACCOUNT_STATUS_UNSYNCED,
                          CHANNEL_ADDED_TO_GUIDE, GROUP_EXAMPLE_KEPT, Account, Alert, Channel,
                          ChannelEvent, ChannelGroup, ChannelGroupEvent, EpgSource,
                          EpgSourceSubscription, Recording)
from tests.support.app import make_test_app
from tests.support.readiness import patched, result
from tests.support.seed import (make_account, make_channel, make_channel_test, make_group,
                                make_recording)

MACHINE_READY = {cid: result(readiness.READY, f'{cid} fine') for cid in setup_steps.MACHINE_CHECKS}
ALL_NAMES = examples.GUIDE_CHANNELS + examples.GROUP_CHANNELS


class _Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.ctx = self.t.app.test_request_context('/')
        self.ctx.push()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def example_account(self, names=ALL_NAMES, **kw):
        kw.setdefault('last_sync_at', datetime.utcnow())
        acc = make_account(name=examples.ACCOUNT_NAME, is_example=True,
                           channel_count=len(names), **kw)
        for n in names:
            make_channel(acc, name=n)
        db.session.commit()
        return acc

    def alerts(self):
        db.session.expire_all()
        return Alert.query.filter_by(alert_type='EXAMPLES_INCOMPLETE').all()


class LoadStartTests(_Case):
    def test_load_adds_one_marked_m3u_account_reading_the_public_links(self):
        with mock.patch.object(examples, '_load') as load:
            resp = self.client.post('/api/examples')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json()['success'])
        load.assert_called_once()
        accounts = Account.query.all()
        self.assertEqual(len(accounts), 1)
        acc = accounts[0]
        self.assertTrue(acc.is_example)
        self.assertEqual((acc.name, acc.account_type, acc.m3u_url),
                         (examples.ACCOUNT_NAME, 'm3u', examples.PLAYLIST_URL))
        # The listings are a url source the account owns and reads - the same shape the Add
        # account form gives an XMLTV URL - so the sync refreshes it like any other.
        src = EpgSource.query.filter_by(owner_account_id=acc.id).one()
        self.assertEqual(src.url, examples.LISTINGS_URL)
        self.assertIsNone(src.refresh_interval_hours)
        self.assertEqual(EpgSourceSubscription.query.filter_by(account_id=acc.id,
                                                               source_id=src.id).count(), 1)

    def test_a_second_load_is_refused_while_examples_are_loaded(self):
        self.example_account()
        with mock.patch.object(examples, '_load') as load:
            resp = self.client.post('/api/examples')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('already loaded', resp.get_json()['error'])
        load.assert_not_called()
        self.assertEqual(Account.query.count(), 1)

    def test_a_load_is_refused_while_one_is_running(self):
        with mock.patch.object(examples, '_load'):
            self.client.post('/api/examples')
            # The stubbed thread never cleared the in-progress mark, as a real one would not
            # have yet.
            resp = self.client.post('/api/examples')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('already loading', resp.get_json()['error'])


class LoadThreadTests(_Case):
    """examples._load with the sync and the health check stubbed."""

    def run_load(self, names=ALL_NAMES, status=ACCOUNT_STATUS_OK, error=None, tested=7):
        acc = make_account(name=examples.ACCOUNT_NAME, is_example=True)
        acc.status = ACCOUNT_STATUS_UNSYNCED  # make_account() always seeds OK
        db.session.commit()
        acc_id = acc.id

        def fake_sync(app, account_id, **_kw):
            with app.app_context():
                a = db.session.get(Account, account_id)
                a.status = status
                a.last_error = error
                a.last_sync_at = datetime.utcnow() if status == ACCOUNT_STATUS_OK else None
                for n in (names if status == ACCOUNT_STATUS_OK else ()):
                    make_channel(a, name=n)
                db.session.commit()

        tests = []
        with mock.patch('app.accounts.run_manual_sync', side_effect=fake_sync), \
                mock.patch('app.channel_tester.run_single_channel_test',
                           side_effect=lambda app, cid: tests.append(cid) or tested):
            examples._load(self.t.app, acc_id)
        db.session.expire_all()
        return acc_id, tests

    def test_a_clean_load_places_every_channel_makes_the_group_and_tests_one(self):
        acc_id, tests = self.run_load()
        in_guide = {c.name for c in Channel.query.filter_by(account_id=acc_id, in_guide=True)}  # guide-flag-read-ok: asserting the rows the load gave
        self.assertEqual(in_guide, set(examples.GUIDE_CHANNELS))
        # Through the one Add to Guide write: each placement leaves its timeline event.
        self.assertEqual(ChannelEvent.query.filter_by(
            event_type=CHANNEL_ADDED_TO_GUIDE).count(), len(examples.GUIDE_CHANNELS))
        group = ChannelGroup.query.filter_by(name=examples.GROUP_NAME).one()
        self.assertTrue(group.is_example)
        self.assertFalse(group.in_guide)
        self.assertEqual(sorted(m.channel.name for m in group.memberships),
                         sorted(examples.GROUP_CHANNELS))
        bbc = Channel.query.filter_by(account_id=acc_id, name=examples.TEST_CHANNEL).one()
        self.assertEqual(tests, [bbc.id])
        self.assertEqual(self.alerts(), [])
        self.assertIsNone(examples.loading())

    def test_a_channel_gone_from_the_list_is_named_and_the_rest_still_load(self):
        names = tuple(n for n in ALL_NAMES if n != 'PBS America')
        acc_id, tests = self.run_load(names=names)
        self.assertEqual(Channel.query.filter_by(account_id=acc_id, in_guide=True).count(),  # guide-flag-read-ok: asserting the rows the load gave
                         len(examples.GUIDE_CHANNELS) - 1)
        self.assertEqual(ChannelGroup.query.filter_by(is_example=True).count(), 1)
        [alert] = self.alerts()
        self.assertIn('PBS America', alert.body)

    def test_one_bloomberg_feed_missing_means_no_group_and_says_so(self):
        names = tuple(n for n in ALL_NAMES if n != 'Bloomberg TV Brasil')
        self.run_load(names=names)
        self.assertEqual(ChannelGroup.query.filter_by(is_example=True).count(), 0)
        [alert] = self.alerts()
        self.assertIn('Bloomberg TV Brasil', alert.body)

    def test_a_group_name_already_taken_is_not_overwritten(self):
        mine = make_group(name=examples.GROUP_NAME, members=[], in_guide=False)
        db.session.commit()
        self.run_load()
        self.assertEqual(ChannelGroup.query.filter_by(name=examples.GROUP_NAME).count(), 1)
        self.assertFalse(db.session.get(ChannelGroup, mine.id).is_example)
        [alert] = self.alerts()
        self.assertIn('already exists', alert.body)

    def test_a_failed_first_sync_names_the_providers_answer_and_places_nothing(self):
        acc_id, tests = self.run_load(status=ACCOUNT_STATUS_ERROR,
                                      error='HTTP 503 from m3upt.com\ntraceback')
        self.assertEqual(Channel.query.count(), 0)
        self.assertEqual(tests, [])
        [alert] = self.alerts()
        self.assertIn('HTTP 503 from m3upt.com', alert.body)
        self.assertNotIn('traceback', alert.body)

    def test_a_health_check_that_could_not_run_is_named(self):
        self.run_load(tested=None)
        [alert] = self.alerts()
        self.assertIn(f'health check on {examples.TEST_CHANNEL} did not run', alert.body)


class DeleteTests(_Case):
    def own_setup(self):
        """An example account and group, plus everything a user might have added since."""
        ex = self.example_account()
        ex_ch = {c.name: c for c in Channel.query.filter_by(account_id=ex.id)}
        mine = make_account(name='Northwind TV', last_sync_at=datetime.utcnow())
        my_ch = make_channel(mine, name='Northwind One')
        group = make_group(name=examples.GROUP_NAME, in_guide=False, recording=False,
                           members=[ex_ch['Bloomberg TV Europe'], ex_ch['Bloomberg TV Brasil']],
                           is_example=True)
        my_group = make_group(name='News feeds', in_guide=False, recording=False,
                              members=[ex_ch['BBC News'], my_ch])
        done = make_recording(status='COMPLETED', name='bbc_evening',
                              channel_id=ex_ch['BBC News'].id,
                              start_time=datetime.utcnow() - timedelta(hours=3),
                              stop_time=datetime.utcnow() - timedelta(hours=2))
        db.session.commit()
        return ex, mine, my_ch, group, my_group, done, ex_ch

    def test_delete_takes_the_examples_and_leaves_everything_of_the_users(self):
        ex, mine, my_ch, group, my_group, done, _ = self.own_setup()
        ex_id, mine_id, group_id, my_group_id, done_id = ex.id, mine.id, group.id, my_group.id, done.id
        resp = self.client.post('/api/examples/remove')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        db.session.expire_all()
        self.assertIsNone(db.session.get(Account, ex_id))
        self.assertEqual(Channel.query.filter_by(account_id=ex_id).count(), 0)
        self.assertIsNone(db.session.get(ChannelGroup, group_id))
        self.assertIsNotNone(db.session.get(Account, mine_id))
        mg = db.session.get(ChannelGroup, my_group_id)
        self.assertEqual([m.channel_id for m in mg.memberships], [my_ch.id])
        self.assertIsNotNone(db.session.get(Recording, done_id))

    def test_the_confirm_dialog_names_what_goes_and_what_stays(self):
        self.own_setup()
        body = self.client.get('/api/examples/removal').get_json()
        text = ' '.join(body['lines'])
        self.assertIn(examples.ACCOUNT_NAME, text)
        self.assertIn(f'The channel group "{examples.GROUP_NAME}"', text)
        self.assertIn('Your group "News feeds" keeps its own channels', text)
        self.assertIn('Recordings you made from example channels stay', text)
        self.assertEqual(body['blockers'], [])

    def test_the_dialog_counts_the_guide_rows_and_checks_that_go_including_the_users(self):
        """BUGS.md 2026-10-26: the page promised "Anything you added yourself stays" while
        the delete took an example channel the user had put in the guide, and its test."""
        ex = self.example_account()
        for ch in Channel.query.filter_by(account_id=ex.id).limit(3):
            ch.in_guide = True
        euronews = make_channel(ex, name='Euronews FR', in_guide=True)
        make_channel_test(euronews)
        db.session.commit()
        text = ' '.join(self.client.get('/api/examples/removal').get_json()['lines'])
        self.assertIn('4 example channels in your TV Guide, including any you put there '
                      'yourself.', text)
        self.assertIn('1 health check result on example channels, including any you ran '
                      'yourself.', text)
        self.assertEqual(self.client.post('/api/examples/remove').status_code, 200)
        db.session.expire_all()
        self.assertIsNone(db.session.get(Channel, euronews.id))

    def test_the_done_message_promises_only_what_stays(self):
        self.own_setup()
        msg = self.client.post('/api/examples/remove').get_json()['message']
        self.assertNotIn('Anything of your own', msg)
        self.assertIn('Your recordings and your own accounts are untouched.', msg)

    def test_an_example_group_the_user_added_to_is_kept_and_its_timeline_says_why(self):
        ex = self.example_account()
        ex_ch = {c.name: c for c in Channel.query.filter_by(account_id=ex.id)}
        mine = make_account(name='Northwind TV')
        my_ch = make_channel(mine, name='Bloomberg (mine)')
        group = make_group(name=examples.GROUP_NAME, in_guide=False, recording=False,
                           members=[ex_ch['Bloomberg TV Europe'], my_ch], is_example=True)
        db.session.commit()
        gid = group.id
        self.assertEqual(self.client.post('/api/examples/remove').status_code, 200)
        db.session.expire_all()
        kept = db.session.get(ChannelGroup, gid)
        self.assertIsNotNone(kept)
        self.assertFalse(kept.is_example)
        self.assertEqual([m.channel_id for m in kept.memberships], [my_ch.id])
        self.assertEqual(ChannelGroupEvent.query.filter_by(
            group_id=gid, event_type=GROUP_EXAMPLE_KEPT).count(), 1)

    def test_a_scheduled_recording_on_an_example_channel_refuses_and_is_named(self):
        ex = self.example_account()
        bbc = Channel.query.filter_by(account_id=ex.id, name='BBC News').one()
        make_recording(status='SCHEDULED', name='bbc_tonight', channel_id=bbc.id,
                       start_time=datetime.utcnow() + timedelta(hours=2),
                       stop_time=datetime.utcnow() + timedelta(hours=3))
        db.session.commit()
        resp = self.client.post('/api/examples/remove')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('"bbc_tonight" is scheduled', resp.get_json()['error'])
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Account, ex.id))
        self.assertEqual(Recording.query.filter_by(name='bbc_tonight').one().status, 'SCHEDULED')

    def test_a_recording_under_way_on_an_example_group_refuses(self):
        ex = self.example_account()
        ex_ch = {c.name: c for c in Channel.query.filter_by(account_id=ex.id)}
        group = make_group(name=examples.GROUP_NAME, in_guide=False,
                           members=[ex_ch['Bloomberg TV Europe'], ex_ch['Bloomberg TV Brasil']],
                           is_example=True)
        make_recording(status='IN_PROGRESS', name='markets_now', group_id=group.id)
        db.session.commit()
        resp = self.client.post('/api/examples/remove')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('"markets_now" is still recording', resp.get_json()['error'])
        self.assertIsNotNone(db.session.get(ChannelGroup, group.id))

    def test_delete_is_refused_while_the_load_is_running(self):
        self.example_account()
        with examples._lock:
            examples._loading.update({'account_id': 1, 'started_at': datetime.utcnow(),
                                      'step': 'Fetching.'})
        resp = self.client.post('/api/examples/remove')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('still loading', resp.get_json()['error'])

    def test_with_no_examples_there_is_nothing_to_delete(self):
        make_account(name='Northwind TV')
        db.session.commit()
        self.assertEqual(self.client.post('/api/examples/remove').status_code, 409)
        self.assertEqual(self.client.get('/api/examples/removal').status_code, 404)
        self.assertEqual(Account.query.count(), 1)

    def test_an_account_is_never_an_example_by_its_name(self):
        """The marker is the only test: an account the user named like the example one
        survives the delete."""
        acc = make_account(name=examples.ACCOUNT_NAME)
        acc.m3u_url = examples.PLAYLIST_URL
        db.session.commit()
        self.assertEqual(self.client.post('/api/examples/remove').status_code, 409)
        self.assertEqual(Account.query.count(), 1)


class PageTests(_Case):
    def get(self, path='/get-started'):
        with patched(**MACHINE_READY):
            return self.client.get(path).get_data(as_text=True)

    def test_a_new_install_is_told_what_the_examples_button_adds(self):
        html = self.get()
        self.assertIn('data-examples-load', html)
        self.assertIn(f'<a href="{examples.SITE_URL}" target="_blank" rel="noopener">M3UPT</a>',
                      html)
        for name in examples.GUIDE_CHANNELS + (examples.GROUP_NAME, examples.ACCOUNT_NAME):
            self.assertIn(name, html)
        self.assertIn('marked as an example', html)

    def test_the_offer_is_gone_once_any_account_exists(self):
        make_account(name='Northwind TV')
        db.session.commit()
        self.assertNotIn('data-examples-load', self.get())

    def test_with_examples_loaded_the_page_says_so_and_offers_the_delete(self):
        self.example_account()
        html = self.get()
        self.assertNotIn('data-examples-load', html)
        self.assertIn('id="gs-examples"', html)
        self.assertIn('data-examples-remove', html)

    def test_no_page_promises_that_what_the_user_added_survives(self):
        """BUGS.md 2026-10-26: the promise against the delete's own behavior. The offer, the
        examples card and the example account's page all say an example channel the user put
        in the guide goes with the examples."""
        offer = self.get()
        ex = self.example_account()
        card = self.get()
        account = self.get(f'/accounts/{ex.id}')
        for name, html in (('offer', offer), ('card', card), ('account page', account)):
            self.assertNotIn('Anything you added yourself stays', html, name)
            self.assertNotIn('leaves anything you added yourself', html, name)
            self.assertIn('put in the guide', html, name)
            self.assertRegex(html, r'[Yy]our\s+recordings\s+stay', name)

    def test_the_offers_button_comes_before_its_list(self):
        """On a phone the list pushed the button a screen and a half down (dev/changelog/1288)."""
        html = self.get()
        self.assertLess(html.index('data-examples-load'), html.index('The button adds:'))

    def test_the_loading_card_says_how_long_without_a_toast(self):
        self.example_account()
        with examples._lock:
            examples._loading.update({'account_id': 1, 'started_at': datetime.utcnow(),
                                      'step': "Fetching M3UPT's channels and listings."})
        self.assertIn('a minute or two', self.get())

    def test_the_empty_guide_offers_the_examples_with_no_account(self):
        html = self.get('/guide')
        self.assertIn('href="/get-started#gs-try"', html)
        self.assertIn('Add an account', html)
        make_account(name='Northwind TV')
        db.session.commit()
        self.assertNotIn('/get-started#gs-try', self.get('/guide'))

    def test_a_running_load_shows_its_step_and_asks_for_the_minute_tick(self):
        self.example_account()
        with examples._lock:
            examples._loading.update({'account_id': 1, 'started_at': datetime.utcnow(),
                                      'step': 'Running a health check on BBC News.'})
        html = self.get()
        self.assertIn('Running a health check on BBC News.', html)
        self.assertIn('data-waiting="1"', html)
        self.assertNotIn('data-examples-remove', html)

    def test_the_example_account_page_says_what_it_is_with_the_delete_beside_it(self):
        ex = self.example_account()
        html = self.get(f'/accounts/{ex.id}')
        self.assertIn('This is the example account', html)
        self.assertIn('data-examples-remove', html)
        self.assertIn('data-examples-keep', html)
        self.assertIn('js/examples.js', html)
        self.assertIn('>Example</span>', self.get('/accounts'))

    def test_an_example_accounts_menus_delete_the_examples_not_the_account(self):
        """A bare account delete would leave the example group behind, empty; the tester
        looked in this menu for the way out (dev/changelog/1288)."""
        ex = self.example_account()
        mine = make_account(name='Northwind TV')
        db.session.commit()
        listing = self.get('/accounts')
        self.assertIn('js/examples.js', listing)
        self.assertEqual(listing.count('data-act="examples-remove"'), 1)
        self.assertEqual(listing.count('data-act="delete"'), 1)
        page = self.get(f'/accounts/{ex.id}')
        self.assertIn('data-act="examples-remove"', page)
        self.assertNotIn('data-act="delete"', page)
        self.assertIn('isExample: true', page)
        self.assertIn('data-act="delete"', self.get(f'/accounts/{mine.id}'))

    def test_a_list_with_no_example_account_loads_no_examples_script(self):
        make_account(name='Northwind TV')
        db.session.commit()
        self.assertNotIn('js/examples.js', self.get('/accounts'))

    def test_an_account_of_the_users_own_carries_none_of_it(self):
        mine = make_account(name='Northwind TV')
        db.session.commit()
        html = self.get(f'/accounts/{mine.id}')
        self.assertNotIn('This is the example account', html)
        self.assertNotIn('js/examples.js', html)


class TrialStepTests(_Case):
    """The examples are a trial: they open the later steps without finishing the account
    step, so Get started and Delete the examples stay in sight (dev/changelog/1288)."""

    def evaluate(self):
        with patched(**MACHINE_READY):
            return setup_steps.evaluate()

    def trial(self, recording=False):
        ex = self.example_account()
        bbc = Channel.query.filter_by(account_id=ex.id, name='BBC News').one()
        bbc.in_guide = True
        if recording:
            make_recording(status='COMPLETED', name='bbc_evening', channel_id=bbc.id,
                           start_time=datetime.utcnow() - timedelta(hours=3),
                           stop_time=datetime.utcnow() - timedelta(hours=2))
        db.session.commit()
        return ex

    def test_only_examples_leave_the_account_step_to_do_and_say_so(self):
        """BUGS.md 2026-10-26: step 1 ticked with no account of the user's own."""
        self.trial()
        setup = self.evaluate()
        step = setup['steps'][0]
        self.assertEqual(step['status'], setup_steps.TODO)
        self.assertIn('Only the example channels so far', step['state'])
        self.assertTrue(step['keep_examples'])
        self.assertEqual(setup['steps'][2]['status'], setup_steps.DONE)

    def test_the_trial_leads_with_its_own_next_step_first(self):
        self.trial()
        self.assertEqual(self.evaluate()['lead']['id'], 'record')

    def test_after_the_first_recording_get_started_stays_on_the_account_step(self):
        """Get started and its menu link went the moment the trial recorded, and Delete the
        examples with them."""
        self.trial(recording=True)
        setup = self.evaluate()
        self.assertFalse(setup['finished'])
        self.assertEqual(setup['done'], 3)
        self.assertEqual(setup['lead']['id'], 'account')
        self.assertEqual(setup_steps.nav_summary()['left'], 1)

    def test_the_trials_account_step_is_accented_only_once_it_is_the_last(self):
        self.trial()
        with patched(**MACHINE_READY):
            looks = [s['display'] for s in setup_steps.page()['steps']]
        self.assertEqual(looks, ['todo', 'done', 'done', 'now'])
        make_recording(status='COMPLETED', name='bbc_evening',
                       start_time=datetime.utcnow() - timedelta(hours=3),
                       stop_time=datetime.utcnow() - timedelta(hours=2))
        db.session.commit()
        with patched(**MACHINE_READY):
            page = setup_steps.page()
        self.assertEqual([s['display'] for s in page['steps']], ['now', 'done', 'done', 'done'])
        html = self.client.get('/get-started').get_data(as_text=True)
        self.assertIn('data-examples-keep', html)

    def test_an_account_of_the_users_own_finishes_the_step_beside_the_examples(self):
        self.trial(recording=True)
        make_account(name='Northwind TV', last_sync_at=datetime.utcnow(), channel_count=48)
        db.session.commit()
        setup = self.evaluate()
        self.assertTrue(setup['finished'])
        self.assertIn('1 account, 48 channels', setup['steps'][0]['state'])
        self.assertIn('example channels are still loaded', setup['steps'][0]['state'])

    def test_an_own_account_still_syncing_is_the_step_even_with_examples_synced(self):
        self.trial()
        acc = make_account(name='Northwind TV')
        acc.status = ACCOUNT_STATUS_UNSYNCED
        db.session.commit()
        step = self.evaluate()['steps'][0]
        self.assertEqual(step['status'], setup_steps.WAITING)
        self.assertIn('Northwind TV', step['state'])


class KeepTests(_Case):
    def test_keep_makes_the_examples_ordinary_and_finishes_the_account_step(self):
        ex = self.example_account()
        ex_ch = {c.name: c for c in Channel.query.filter_by(account_id=ex.id)}
        group = make_group(name=examples.GROUP_NAME, in_guide=False, recording=False,
                           members=[ex_ch['Bloomberg TV Europe'], ex_ch['Bloomberg TV Brasil']],
                           is_example=True)
        db.session.commit()
        ex_id, gid = ex.id, group.id
        resp = self.client.post('/api/examples/keep')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertIn('yours now', resp.get_json()['message'])
        db.session.expire_all()
        self.assertFalse(db.session.get(Account, ex_id).is_example)
        self.assertFalse(db.session.get(ChannelGroup, gid).is_example)
        event = ChannelGroupEvent.query.filter_by(group_id=gid,
                                                  event_type=GROUP_EXAMPLE_KEPT).one()
        self.assertIn('Keep the examples', event.detail)
        with patched(**MACHINE_READY):
            self.assertEqual(setup_steps.evaluate()['steps'][0]['status'], setup_steps.DONE)
        # Nothing is an example any more, so there is nothing for the delete to take.
        self.assertEqual(self.client.post('/api/examples/remove').status_code, 409)
        self.assertIsNotNone(db.session.get(Account, ex_id))

    def test_keep_is_refused_while_the_load_is_running(self):
        ex = self.example_account()
        with examples._lock:
            examples._loading.update({'account_id': ex.id, 'started_at': datetime.utcnow(),
                                      'step': 'Fetching.'})
        resp = self.client.post('/api/examples/keep')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('still loading', resp.get_json()['error'])
        db.session.expire_all()
        self.assertTrue(db.session.get(Account, ex.id).is_example)

    def test_with_no_examples_there_is_nothing_to_keep(self):
        make_account(name='Northwind TV')
        db.session.commit()
        self.assertEqual(self.client.post('/api/examples/keep').status_code, 409)

    def test_keeping_throws_the_menus_cached_count_away(self):
        """The account count does not move, so the key has to see the marker."""
        self.example_account()
        before = setup_steps.state_key()
        self.client.post('/api/examples/keep')
        self.assertNotEqual(setup_steps.state_key(), before)


class MigrationTests(unittest.TestCase):
    def test_m099_adds_both_markers_false_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
            cur = conn.cursor()
            cur.execute('CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT)')
            cur.execute('CREATE TABLE channel_groups (id INTEGER PRIMARY KEY, name TEXT)')
            cur.execute("INSERT INTO accounts (name) VALUES ('Northwind TV')")
            cur.execute("INSERT INTO channel_groups (name) VALUES ('News')")
            conn.commit()
            M._m099_is_example(conn, cur)
            M._m099_is_example(conn, cur)
            self.assertEqual(cur.execute('SELECT is_example FROM accounts').fetchone(), (0,))
            self.assertEqual(cur.execute('SELECT is_example FROM channel_groups').fetchone(),
                             (0,))
            conn.close()

    def test_m099_registered_at_its_own_version(self):
        registered = {v: fn for v, _d, fn in M.SCHEMA_MIGRATIONS}
        self.assertIs(registered.get(99), M._m099_is_example)


if __name__ == '__main__':
    unittest.main()
