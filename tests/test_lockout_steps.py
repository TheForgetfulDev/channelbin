"""Each wrong-login lockout of one address is longer than its last (app/auth.py::FailureTracker,
dev/changelog/1774): the steps, what forgets them, the size limit, and the IPTV Output page's
one button per address."""
import unittest
from datetime import datetime, timedelta
from unittest import mock

import app.output_feed as feed
from app import auth
from tests.support.output_feed import FeedCase

WINDOW = timedelta(minutes=15)


class _Clock:
    """A settable clock in place of app.auth's datetime, so a day of lockouts takes no time."""

    def __init__(self, case):
        self.now = datetime(2026, 11, 27, 12, 0, 0)
        fake = mock.patch.object(auth, 'datetime', mock.Mock(utcnow=lambda: self.now))
        fake.start()
        case.addCleanup(fake.stop)

    def forward(self, **by):
        self.now += timedelta(**by)


class _Tracked(unittest.TestCase):
    TRIES = 3

    def setUp(self):
        self.clock = _Clock(self)
        self.tracker = auth.FailureTracker(self.TRIES, WINDOW)

    def trip(self, address='203.0.113.9', tracker=None):
        """Wrong logins until `address` is locked out. Returns its Lockout."""
        tracker = tracker or self.tracker
        tripped = [tracker.record_failure(address) for _ in range(self.TRIES)]
        self.assertEqual(tripped, [False] * (self.TRIES - 1) + [True])
        return tracker.lockout(address)

    def wait_out(self, found, **more):
        self.clock.now = found.until + timedelta(**more)


class StepTests(_Tracked):
    def test_each_lockout_of_one_address_is_longer_than_its_last(self):
        lengths = []
        for _ in range(len(auth.LOCKOUT_STEPS) + 2):
            found = self.trip()
            lengths.append(found.length)
            self.assertEqual(found.until, self.clock.now + found.length)
            self.wait_out(found)
        self.assertEqual(lengths, list(auth.LOCKOUT_STEPS) + [auth.LOCKOUT_STEPS[-1]] * 2)
        self.assertEqual([m.total_seconds() // 60 for m in auth.LOCKOUT_STEPS],
                         [5, 15, 30, 60, 360, 720, 1440])

    def test_a_lockout_runs_its_whole_step_from_the_login_that_tripped_it(self):
        # Wrong logins spread over 14 minutes: counted from the first of them, as it was
        # before, the lockout would end a minute after it began.
        for _ in range(self.TRIES - 1):
            self.tracker.record_failure('203.0.113.9')
            self.clock.forward(minutes=7)
        self.assertTrue(self.tracker.record_failure('203.0.113.9'))
        self.clock.forward(minutes=4, seconds=59)
        self.assertIsNotNone(self.tracker.check_lockout('203.0.113.9'))
        self.clock.forward(seconds=1)
        self.assertIsNone(self.tracker.check_lockout('203.0.113.9'))

    def test_a_wrong_login_during_a_lockout_counts_for_nothing(self):
        found = self.trip()
        for _ in range(self.TRIES * 3):
            self.assertFalse(self.tracker.record_failure('203.0.113.9'))
        self.assertEqual(self.tracker.lockout('203.0.113.9'), found)
        # And the lockout starts the count again from none.
        self.wait_out(found)
        self.assertFalse(self.tracker.record_failure('203.0.113.9'))
        self.assertIsNone(self.tracker.check_lockout('203.0.113.9'))

    def test_wrong_logins_older_than_the_window_do_not_count(self):
        for _ in range(self.TRIES - 1):
            self.tracker.record_failure('203.0.113.9')
        self.clock.forward(minutes=15)
        self.assertFalse(self.tracker.record_failure('203.0.113.9'))

    def test_one_64_shares_its_steps(self):
        first = self.trip('2001:db8:1:2::1')
        self.wait_out(first)
        again = [self.tracker.record_failure(f'2001:db8:1:2::{n + 2:x}') for n in range(self.TRIES)]
        self.assertTrue(again[-1])
        found = self.tracker.lockout('2001:db8:1:2:ffff::1')
        self.assertEqual((found.address, found.times, found.length),
                         ('2001:db8:1:2::/64', 2, auth.LOCKOUT_STEPS[1]))

    def test_another_address_starts_at_the_first_step(self):
        self.wait_out(self.trip())
        self.trip()
        self.assertEqual(self.trip('203.0.113.10').length, auth.LOCKOUT_STEPS[0])


class ForgetTests(_Tracked):
    def test_a_clean_day_after_a_lockout_ends_forgets_the_steps(self):
        self.wait_out(self.trip(), hours=24)
        self.assertEqual(self.trip().times, 1)

    def test_less_than_a_clean_day_does_not(self):
        self.wait_out(self.trip(), hours=23, minutes=59)
        found = self.trip()
        self.assertEqual((found.times, found.length), (2, auth.LOCKOUT_STEPS[1]))

    def test_the_day_is_counted_from_the_end_of_the_latest_lockout(self):
        self.wait_out(self.trip(), hours=20)
        second = self.trip()
        # 24 hours after the first ended, but not after the second.
        self.wait_out(second, hours=20)
        self.assertEqual(self.trip().times, 3)

    def test_a_right_password_at_a_one_password_door_forgets_the_steps(self):
        self.wait_out(self.trip())
        self.tracker.record_success('203.0.113.9')
        self.assertEqual(self.tracker.lockouts, {})
        self.assertEqual(self.trip().times, 1)

    def test_clearing_one_address_forgets_its_steps_and_nobody_elses(self):
        self.wait_out(self.trip())
        self.trip()
        other = self.trip('2001:db8:1:2::1')
        # By the key the page names it with, which for IPv6 is no address.
        self.assertTrue(self.tracker.clear('203.0.113.9'))
        self.assertFalse(self.tracker.clear('203.0.113.9'))
        self.assertEqual(self.tracker.locked_out(), [other])
        self.assertEqual(self.trip().times, 1)
        self.assertTrue(self.tracker.clear(other.address))
        self.assertEqual(list(self.tracker.lockouts), ['203.0.113.9'])

    def test_reset_forgets_everything(self):
        self.trip()
        self.tracker.reset()
        self.assertEqual((self.tracker.failures, self.tracker.lockouts), ({}, {}))


class BoundsTests(_Tracked):
    TRIES = 1

    def test_the_memory_of_lockouts_is_held_to_the_size_limit(self):
        tracker = auth.FailureTracker(1, WINDOW, max_tracked=20)
        for n in range(60):
            self.clock.forward(seconds=1)
            tracker.record_failure(f'198.51.100.{n}')
        self.assertEqual(len(tracker.lockouts), 20)
        # The ones with the most left to run are the ones kept.
        self.assertIn('198.51.100.59', tracker.lockouts)
        self.assertNotIn('198.51.100.0', tracker.lockouts)

    def test_a_new_lockout_is_never_the_one_dropped_from_a_full_table(self):
        tracker = auth.FailureTracker(1, WINDOW, max_tracked=5)
        for n in range(5):
            # Each on its 4th lockout, an hour long.
            tracker.lockouts[f'198.51.100.{n}'] = (3, self.clock.now - timedelta(seconds=1))
            tracker.record_failure(f'198.51.100.{n}')
        self.assertTrue(tracker.record_failure('203.0.113.9'))
        self.assertEqual(tracker.lockout('203.0.113.9').length, auth.LOCKOUT_STEPS[0])
        self.assertEqual(len(tracker.lockouts), 5)

    def test_a_forgotten_lockout_is_dropped_by_any_later_call(self):
        self.wait_out(self.trip(), hours=24)
        self.assertIsNone(self.tracker.check_lockout('192.0.2.200'))
        self.assertEqual(self.tracker.lockouts, {})

    def test_an_address_that_only_asks_is_never_tracked(self):
        for n in range(50):
            self.tracker.check_lockout(f'192.0.2.{n}')
        self.assertEqual((self.tracker.failures, self.tracker.lockouts), ({}, {}))


class WordsTests(unittest.TestCase):
    def test_how_long_and_which_one(self):
        def words(times, length):
            return auth.lockout_words(auth.Lockout('a', datetime.utcnow(), times, length))
        self.assertEqual(words(1, timedelta(minutes=5)), '5 minutes, its first lockout')
        self.assertEqual(words(2, timedelta(minutes=15)), '15 minutes, its 2nd lockout in a row')
        self.assertEqual(words(3, timedelta(hours=1)), '1 hour, its 3rd lockout in a row')
        self.assertEqual(words(11, timedelta(hours=24)), '24 hours, its 11th lockout in a row')
        self.assertEqual(words(21, timedelta(hours=24)), '24 hours, its 21st lockout in a row')

    def test_the_steps_in_a_sentence(self):
        self.assertEqual(auth.steps_words(),
                         '5 minutes, then 15 minutes, then 30 minutes, then 1 hour, then 6 hours, '
                         'then 12 hours, then 24 hours every time after that')

    def test_both_doors_use_the_same_steps(self):
        self.assertEqual(auth.web_login.steps, auth.LOCKOUT_STEPS)
        self.assertEqual(feed.login_failures.steps, auth.LOCKOUT_STEPS)


class OutputDoorTests(FeedCase):
    def setUp(self):
        super().setUp()
        self.clock = _Clock(self)

    def wrong(self, n=feed.LOGIN_MAX_FAILURES):
        for _ in range(n):
            self.assertEqual(self.get('/player_api.php', password='nope').status_code, 401)

    def page(self):
        return self.client.get('/iptv-output').get_data(as_text=True)

    def test_the_log_says_how_long_and_which_lockout(self):
        with self.assertLogs('app.routes.output_feed', 'WARNING') as logs:
            self.wrong()
        self.assertIn('locked out for 5 minutes, its first lockout after 10 wrong logins', logs.output[0])
        self.clock.forward(minutes=5)
        with self.assertLogs('app.routes.output_feed', 'WARNING') as logs:
            self.wrong()
        self.assertIn('locked out for 15 minutes, its 2nd lockout in a row', logs.output[0])

    def test_a_right_login_between_lockouts_forgets_nothing(self):
        self.wrong()
        self.clock.forward(minutes=5)
        self.assertEqual(self.get('/player_api.php').status_code, 200)
        self.wrong()
        self.assertEqual(feed.login_failures.lockout('127.0.0.1').times, 2)
        self.assertEqual(self.get('/player_api.php').status_code, 401)

    def test_the_page_says_how_long_each_is_and_what_the_steps_are(self):
        self.wrong()
        self.clock.forward(minutes=5)
        self.wrong()
        page = self.page()
        self.assertIn('127.0.0.1 is locked out until', page)
        self.assertIn('15 minutes, its 2nd lockout in a row', page)
        self.assertIn(auth.steps_words(), page)
        self.assertIn('when ChannelBin restarts', page)
        self.assertNotIn('lockouts-clear', page)

    def test_each_address_has_its_own_button_and_it_frees_only_that_one(self):
        self.wrong()
        for _ in range(feed.LOGIN_MAX_FAILURES):
            feed.login_failures.record_failure('2001:db8:1:2::7')
        page = self.page()
        self.assertEqual(page.count('data-act="lockout-clear"'), 2)
        self.assertIn('data-address="2001:db8:1:2::/64"', page)
        resp = self.client.post('/api/iptv-output/lockouts/clear', json={'address': '2001:db8:1:2::/64'})
        self.assertEqual(resp.get_json(), {'success': True, 'cleared': True})
        self.assertEqual([l.address for l in feed.login_failures.locked_out()], ['127.0.0.1'])
        self.assertEqual(self.get('/player_api.php').status_code, 401)

    def test_no_call_frees_every_address(self):
        self.wrong()
        for body in (None, {}, {'address': ''}, {'address': ['127.0.0.1']}):
            resp = self.client.post('/api/iptv-output/lockouts/clear', json=body)
            self.assertEqual(resp.status_code, 400, body)
            self.assertIn('error', resp.get_json())
        self.assertEqual(len(feed.login_failures.locked_out()), 1)

    def test_a_flood_of_lockouts_is_listed_in_part_and_counted(self):
        from app import output_feed_view
        for n in range(output_feed_view.LOCKOUTS_SHOWN + 7):
            feed.login_failures.lockouts[f'198.51.100.{n}'] = (
                1, self.clock.now + timedelta(minutes=1, seconds=n))
        page = self.page()
        self.assertEqual(page.count('data-act="lockout-clear"'), output_feed_view.LOCKOUTS_SHOWN)
        self.assertIn('7 more addresses are locked out', page)
        self.assertIn('data-address="198.51.100.0"', page)

    def test_a_lockout_that_ends_tomorrow_says_so(self):
        feed.login_failures.lockouts['203.0.113.9'] = (7, self.clock.now + timedelta(hours=24))
        with mock.patch('app.output_feed_view.datetime', mock.Mock(utcnow=lambda: self.clock.now)):
            page = self.page()
        self.assertRegex(page, r'203\.0\.113\.9 is locked out until [0-9: APM]+ tomorrow')
