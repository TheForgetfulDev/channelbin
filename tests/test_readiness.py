"""The Readiness check: the registries, the evaluation, and silencing a check.

Built in dev/changelog/950 from dev/mockups/40-readiness.html. Every case here guards a
rule the feature is built on rather than a detail of one check's wording:

  * A check that costs a process, a provider connection or a real message NEVER runs
    because somebody loaded the page. That is the whole reason the card can live on
    Maintenance at all, and the only thing standing between it and the hidden-I/O defect
    class is that `evaluate()` does not call those checks.
  * A check that could not answer is never rendered as a pass, and a check that raised is
    reported rather than swallowed.
  * Every check stands behind at least one capability. The capability list is the only view
    of the checks the page draws, so an orphaned check is a check nobody can see.
  * Silencing is a registry flag, enforced server-side. A check whose failure means the
    install cannot do its job may not be silenced however the request is shaped.
  * A silenced check stops counting - toward the verdict, toward the capability it sits
    behind, and toward the count beside Maintenance - and is still reported.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_readiness
"""
import contextlib
import json
import unittest
from unittest import mock

from app import db, readiness
from app.database import UserPref
from tests.support import seed
from tests.support.app import make_test_app


@contextlib.contextmanager
def patched(**results):
    """Swap one or more checks for fixed answers, by check id.

    The registry holds a direct reference to each check's function, so patching the module
    attribute would not reach it - the tuple is what has to be replaced.
    """
    swapped = tuple(
        c._replace(run=(lambda result: lambda ctx: result)(results[c.id]))
        if c.id in results else c
        for c in readiness.CHECKS)
    unknown = sorted(set(results) - {c.id for c in readiness.CHECKS})
    assert not unknown, f'no such check: {unknown}'
    with mock.patch.object(readiness, 'CHECKS', swapped), \
            mock.patch.dict(readiness.CHECKS_BY_ID, {c.id: c for c in swapped}):
        yield


def result(status, found='found', tested='tested'):
    return readiness.Result(status, tested, found)


class RegistryTests(unittest.TestCase):
    """The two registries, checked against each other. No app needed."""

    def test_every_check_id_is_unique(self):
        ids = [c.id for c in readiness.CHECKS]
        self.assertEqual(sorted(ids), sorted(set(ids)))

    def test_every_check_stands_behind_at_least_one_capability(self):
        behind = {cid for cap in readiness.CAPABILITIES for cid in cap.needs}
        orphans = sorted(c.id for c in readiness.CHECKS if c.id not in behind)
        self.assertEqual(
            orphans, [],
            'The capability list is the only view of the checks the card draws, so a check '
            f'behind no capability is one nobody can ever see: {orphans}')

    def test_every_capability_names_real_checks(self):
        unknown = sorted({cid for cap in readiness.CAPABILITIES for cid in cap.needs
                          if cid not in readiness.CHECKS_BY_ID})
        self.assertEqual(unknown, [])

    def test_every_check_has_a_label_a_noun_and_a_consequence(self):
        for check in readiness.CHECKS:
            with self.subTest(check=check.id):
                self.assertTrue(check.label, 'a check has to state its own claim')
                self.assertTrue(check.short, 'a check needs a noun for the reason sentence')
                self.assertTrue(check.without, 'principle 1: name what stops working')
                self.assertIn(check.cost, (readiness.CHEAP, readiness.ON_DEMAND))
                self.assertIn(check.area, {aid for aid, _label in readiness.AREAS})

    def test_every_status_has_a_rank(self):
        """A state with no rank silently sorts as whatever the dict default would be."""
        for status in readiness.STATUSES:
            self.assertIn(status, readiness.STATUS_RANK)

    def test_every_capability_state_has_a_mark_and_a_colour(self):
        for state in readiness.CAP_STATES:
            self.assertIn(state, readiness.CAP_MARK)
            self.assertIn(state, readiness.CAP_HEALTH)

    def test_the_checks_that_cost_something_are_the_three_that_were_designed_that_way(self):
        """A cheap check that becomes expensive has to be declared ON_DEMAND in the same
        edit, or the card starts spawning processes because a page was opened."""
        self.assertEqual(
            sorted(c.id for c in readiness.CHECKS if c.cost == readiness.ON_DEMAND),
            ['account_login', 'ffmpeg_build', 'notify_delivers'])

    def test_nothing_that_means_the_install_is_broken_can_be_silenced(self):
        """Which checks may be silenced is the one judgment call in the feature, so it is
        pinned here rather than left to whoever edits the registry next."""
        self.assertEqual(
            sorted(c.id for c in readiness.CHECKS if not c.ignorable),
            ['accounts_any', 'auth_gate', 'db_local', 'db_write', 'ffmpeg', 'ffprobe',
             'guide_content', 'guide_groups', 'scheduler', 'secret_key', 'storage_dvr',
             'timezone'])


class _AppCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()


class EvaluationTests(_AppCase):
    def test_an_ondemand_check_reports_not_run_and_is_never_called(self):
        """The rule the whole card rests on: opening the page spawns no process, opens no
        provider connection and sends no message."""
        called = []

        def boom(ctx):
            called.append(1)
            return result(readiness.READY)

        checks = tuple(c._replace(run=boom) if c.cost == readiness.ON_DEMAND else c
                       for c in readiness.CHECKS)
        with mock.patch.object(readiness, 'CHECKS', checks), \
                mock.patch.dict(readiness.CHECKS_BY_ID, {c.id: c for c in checks}):
            payload = readiness.evaluate()
        self.assertEqual(called, [], 'an on-demand check ran on a plain evaluation')
        row = next(r for r in payload['checks'] if r['id'] == 'account_login')
        self.assertEqual(row['status'], readiness.NOT_RUN)

    def test_an_ondemand_check_that_has_not_run_does_not_drag_its_capability_down(self):
        """Otherwise "Record a program right now" is grey on every load, because the
        provider-login check sits behind it - the opposite of "nothing expensive happens
        because you looked".

        Every other check behind the capability is answered green, so the not-run one is
        the only thing that could move it. Without that, a throwaway install's missing
        accounts and missing /dvr turn the row red before this claim is reached at all.
        """
        with patched(**{c.id: result(readiness.READY) for c in readiness.CHECKS
                        if c.cost == readiness.CHEAP}):
            payload = readiness.evaluate()
        record = next(c for c in payload['capabilities'] if c['id'] == 'record')
        self.assertIn('account_login', record['not_run'])
        self.assertEqual(record['state'], readiness.CAN,
                         'a check nobody asked for must not grey out the capability')

    def test_a_check_that_could_not_answer_is_never_a_pass(self):
        fine = result(readiness.READY)
        answers = {c.id: fine for c in readiness.CHECKS}
        answers['storage_space'] = result(readiness.UNKNOWN, 'no honest answer')
        with patched(**answers):
            payload = readiness.evaluate()
        row = next(r for r in payload['checks'] if r['id'] == 'storage_space')
        self.assertEqual(row['status'], readiness.UNKNOWN)
        record = next(c for c in payload['capabilities'] if c['id'] == 'record')
        self.assertEqual(record['state'], readiness.CAP_UNKNOWN,
                         'a check that could not answer must never leave a capability green')

    def test_a_check_that_raises_is_reported_not_swallowed(self):
        def boom(ctx):
            raise RuntimeError('the probe exploded')

        checks = tuple(c._replace(run=boom) if c.id == 'db_write' else c
                       for c in readiness.CHECKS)
        with mock.patch.object(readiness, 'CHECKS', checks):
            payload = readiness.evaluate()
        row = next(r for r in payload['checks'] if r['id'] == 'db_write')
        self.assertEqual(row['status'], readiness.UNKNOWN)
        self.assertIn('the probe exploded', row['found'])

    def test_a_capability_with_nothing_to_look_at_is_not_a_yes(self):
        payload = self._with_status('guide_groups', readiness.NOTHING)
        failover = next(c for c in payload['capabilities'] if c['id'] == 'failover')
        self.assertEqual(failover['state'], readiness.NOT_SET_UP)

    def test_a_blocked_capability_names_every_blocker_not_just_the_first(self):
        """Round 2's correction (dev/changelog/950): several checks can block one capability,
        and round 1 admitted to one of them."""
        with patched(ffmpeg=result(readiness.PROBLEM, 'gone'),
                     storage_dvr=result(readiness.PROBLEM, 'gone'),
                     # A throwaway install has no accounts and no /dvr, so the rest of the
                     # capability is answered too - this is about which blockers are named.
                     accounts_any=result(readiness.READY),
                     storage_space=result(readiness.READY),
                     guide_content=result(readiness.READY)):
            payload = readiness.evaluate()
        record = next(c for c in payload['capabilities'] if c['id'] == 'record')
        self.assertEqual(record['state'], readiness.CANNOT)
        self.assertEqual(sorted(record['blockers']), ['ffmpeg', 'storage_dvr'])

    def test_the_capability_order_is_the_registry_order_and_record_is_first(self):
        payload = readiness.evaluate()
        self.assertEqual([c['id'] for c in payload['capabilities']],
                         [c.id for c in readiness.CAPABILITIES])
        self.assertEqual(payload['capabilities'][0]['id'], 'record')

    def test_a_check_offers_one_click_or_a_link_but_never_both(self):
        payload = readiness.evaluate()
        for row in payload['checks']:
            with self.subTest(check=row['id']):
                self.assertFalse(row['action'] and row['link'],
                                 'act in place OR send them there, never both on one row')

    def test_the_verdict_says_not_ready_and_names_what_it_costs(self):
        with patched(ffmpeg=result(readiness.PROBLEM, 'It could not be run')):
            payload = readiness.evaluate()
        self.assertEqual(payload['verdict']['level'], 'bad')
        self.assertTrue(payload['verdict']['head'].startswith('Not ready: you cannot '))
        self.assertEqual(payload['verdict']['sub'], readiness.CHECKS_BY_ID['ffmpeg'].without)

    def test_the_nav_counts_capabilities_not_checks(self):
        """One broken binary blocks four capabilities; a tally of checks would read as four
        alarms for one problem, and a tally of capabilities is the number you can act on."""
        with patched(ffmpeg=result(readiness.PROBLEM, 'gone')):
            payload = readiness.evaluate()
        blocked = [c['id'] for c in payload['capabilities'] if c['state'] == readiness.CANNOT]
        self.assertEqual(payload['nav']['blocked'], len(blocked))
        self.assertGreater(len(blocked), 1)

    def _with_status(self, check_id, status):
        with patched(**{check_id: result(status)}):
            return readiness.evaluate()


class SilencingTests(_AppCase):
    """A degraded check the user does not care about stops counting (dev/changelog/950).

    Dimmed, never hidden; the state it actually found is still reported; and a green
    verdict is reachable with something silenced, which is the point.
    """

    def test_an_ignored_check_stops_blocking_its_capability(self):
        readiness.set_check_ignored('notify_any', True)
        with patched(notify_any=result(readiness.ATTENTION, 'nothing is switched on')):
            payload = readiness.evaluate()
        notify = next(c for c in payload['capabilities'] if c['id'] == 'notify')
        self.assertEqual(notify['degraded'], [])
        self.assertIn('notify_any', notify['ignored'])

    def test_an_ignored_check_still_reports_what_it_found(self):
        readiness.set_check_ignored('notify_any', True)
        with patched(notify_any=result(readiness.ATTENTION, 'nothing is switched on')):
            payload = readiness.evaluate()
        row = next(r for r in payload['checks'] if r['id'] == 'notify_any')
        self.assertTrue(row['ignored'])
        self.assertEqual(row['status'], readiness.ATTENTION,
                         'silencing must never rewrite the state the check actually found')
        self.assertEqual(row['found'], 'nothing is switched on')

    def test_an_ignored_check_is_off_the_nav_count(self):
        fine = result(readiness.READY)
        others = {c.id: fine for c in readiness.CHECKS if c.id != 'alerts_open'}
        with patched(alerts_open=result(readiness.PROBLEM, '3 open'), **others):
            before = readiness.evaluate()['nav']
            readiness.set_check_ignored('alerts_open', True)
            after = readiness.evaluate()['nav']
        self.assertEqual(before['blocked'], 1)
        self.assertEqual(after['blocked'], 0)

    def test_the_verdict_can_be_green_with_something_ignored_and_says_so(self):
        readiness.set_check_ignored('notify_any', True)
        payload = self._everything_else_fine()
        self.assertEqual(payload['verdict']['head'],
                         'Ready: everything you would want to do works')
        self.assertEqual(payload['counts']['ignored'], 1)

    def test_a_check_that_means_the_install_is_broken_cannot_be_silenced(self):
        with self.assertRaises(ValueError):
            readiness.set_check_ignored('ffmpeg', False)
        with self.assertRaises(ValueError):
            readiness.set_check_ignored('storage_dvr', True)

    def test_an_unknown_check_id_is_refused(self):
        with self.assertRaises(KeyError):
            readiness.set_check_ignored('no_such_check', True)

    def test_it_persists_server_side_so_it_follows_the_user_across_browsers(self):
        readiness.set_check_ignored('search_index', True)
        pref = db.session.get(UserPref, readiness.IGNORED_PREF_KEY)
        self.assertEqual(json.loads(pref.value), ['search_index'])
        self.assertEqual(readiness.ignored_check_ids(), {'search_index'})

    def test_an_ignore_persists_even_once_the_check_passes(self):
        """Deliberate (dev/changelog/950): an ignore is the user's answer and only the user
        takes it back. The dimmed row still shows the real state either way."""
        readiness.set_check_ignored('search_index', True)
        with patched(search_index=result(readiness.READY, 'fine')):
            readiness.evaluate()
        self.assertEqual(readiness.ignored_check_ids(), {'search_index'})

    def test_a_stored_id_that_is_no_longer_a_check_is_dropped(self):
        db.session.add(UserPref(key=readiness.IGNORED_PREF_KEY,
                                value=json.dumps(['search_index', 'retired_check'])))
        db.session.commit()
        self.assertEqual(readiness.ignored_check_ids(), {'search_index'})

    def _everything_else_fine(self):
        answers = {c.id: result(readiness.READY, 'fine') for c in readiness.CHECKS}
        answers['notify_any'] = result(readiness.ATTENTION, 'nothing is switched on')
        with patched(**answers):
            # The on-demand checks read as not-run until they are asked for, which would
            # change the verdict, so they are answered the way a completed Run all leaves
            # them.
            for cid in ('ffmpeg_build', 'account_login', 'notify_delivers'):
                readiness.run_check(cid)
            return readiness.evaluate()


class RunTests(_AppCase):
    def test_running_a_check_stores_its_answer_and_it_stops_being_not_run(self):
        with patched(ffmpeg_build=result(readiness.READY, 'all present')):
            payload = readiness.run_check('ffmpeg_build')
        row = next(r for r in payload['checks'] if r['id'] == 'ffmpeg_build')
        self.assertEqual(row['status'], readiness.READY)
        self.assertEqual(row['found'], 'all present')
        self.assertTrue(row['last_run'])

    def test_a_cheap_check_cannot_be_asked_for(self):
        with self.assertRaises(ValueError):
            readiness.run_check('db_write')

    def test_pending_lists_only_what_has_not_been_asked_for(self):
        self.assertEqual(readiness.pending_ondemand_ids(),
                         ['ffmpeg_build', 'account_login', 'notify_delivers'])
        with patched(ffmpeg_build=result(readiness.READY, 'ok')):
            readiness.run_check('ffmpeg_build')
        self.assertEqual(readiness.pending_ondemand_ids(),
                         ['account_login', 'notify_delivers'])

    def test_the_nav_summary_never_runs_an_ondemand_check(self):
        """A 15-second poll in three open tabs must not be able to spawn a process or open
        a provider connection."""
        called = []

        def boom(ctx):
            called.append(1)
            return result(readiness.READY)

        checks = tuple(c._replace(run=boom) if c.cost == readiness.ON_DEMAND else c
                       for c in readiness.CHECKS)
        with mock.patch.object(readiness, 'CHECKS', checks), \
                mock.patch.dict(readiness.CHECKS_BY_ID, {c.id: c for c in checks}):
            readiness.nav_summary()
        self.assertEqual(called, [])


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        # The routes are CSRF-protected app-wide; that is asserted once in
        # tests/test_csrf_envelope.py and is not what these cases are about.
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def test_the_report_endpoint_answers(self):
        resp = self.client.get('/api/readiness')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['checks']), len(readiness.CHECKS))
        self.assertEqual(len(data['capabilities']), len(readiness.CAPABILITIES))

    def test_the_report_never_carries_a_secret(self):
        """flask.secret_key and auth.password_hash are sensitive config leaves, so the
        signing-key check reports a boolean and the notification checks report names."""
        body = self.client.get('/api/readiness').get_data(as_text=True)
        self.assertNotIn(self.t.app.config['SECRET_KEY'], body)

    def test_an_unknown_check_is_a_404(self):
        resp = self.client.post('/api/readiness/run', json={'check': 'nope'})
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.get_json())

    def test_a_cheap_check_cannot_be_asked_for_over_the_wire(self):
        resp = self.client.post('/api/readiness/run', json={'check': 'db_write'})
        self.assertEqual(resp.status_code, 400)

    def test_silencing_a_check_that_may_not_be_silenced_is_refused_by_the_route(self):
        """Enforcement is server-side: the card draws no Ignore button for these, and that
        is not what stops the request."""
        resp = self.client.post('/api/readiness/ignore',
                                json={'check': 'ffmpeg', 'ignored': True})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())
        with self.t.app.app_context():
            self.assertEqual(readiness.ignored_check_ids(), set())

    def test_silencing_round_trips_through_the_route(self):
        resp = self.client.post('/api/readiness/ignore',
                                json={'check': 'notify_any', 'ignored': True})
        self.assertEqual(resp.status_code, 200)
        row = next(r for r in resp.get_json()['checks'] if r['id'] == 'notify_any')
        self.assertTrue(row['ignored'])
        resp = self.client.post('/api/readiness/ignore',
                                json={'check': 'notify_any', 'ignored': False})
        row = next(r for r in resp.get_json()['checks'] if r['id'] == 'notify_any')
        self.assertFalse(row['ignored'])

    def test_the_nav_poll_carries_the_readiness_counts(self):
        data = self.client.get('/api/nav-status').get_json()
        self.assertIn('readiness', data)
        self.assertIn('blocked', data['readiness'])
        self.assertIn('degraded', data['readiness'])


class GuideGroupReadTests(unittest.TestCase):
    """The read-side guide-group invariant the check calls, rather than re-deriving it."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_it_names_the_guide_groups_with_nobody_switched_on(self):
        from app.channel_groups import guide_groups_missing_recording_member
        acc = seed.make_account()
        ch = seed.make_channel(acc, name='A feed')
        good = seed.make_group(name='Good', members=[ch])
        bad = seed.make_group(name='Bad', members=[seed.make_channel(acc, name='B feed')])
        good.in_guide = True
        bad.in_guide = True
        for m in good.memberships:
            m.recording_enabled = True
        for m in bad.memberships:
            m.recording_enabled = False
        db.session.commit()
        broken, total = guide_groups_missing_recording_member()
        self.assertEqual([g.name for g in broken], ['Bad'])
        self.assertEqual(total, 2)


if __name__ == '__main__':
    unittest.main()
