"""Tier 2 - the health check run's estimated time remaining (dev/changelog/1067).

The display shipped on 2026-06-27 and was deleted on 2026-07-17 with the page it lived on,
so the invariants here are as much about it staying put as about the arithmetic: the
estimate is computed once on the server, it is suppressed until it has measured something,
it may not lurch between samples, and a run that stops making progress makes it grow rather
than sit still.

No app and no database - _advance_eta() reads module state and a clock, so the tests set
that state directly and move the clock by backdating the run's own timestamps.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.channel_tester as ct  # noqa: E402
from app.postprocessor import EtaSmoother  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402


class _Run:
    """Drives a synthetic run through _advance_eta() with a controllable clock.

    `wall` is seconds since the run started and `in_flight` seconds into the current
    channel; both are applied by backdating the timestamps _advance_eta() measures
    against, which is what keeps the production code free of a test-only clock seam.
    """

    def __init__(self, total, nominal=35.0):
        ct._state = ct.RunState(
            is_running=True,
            current_phase='testing',
            total_channels=total,
            nominal_channel_seconds=nominal,
        )
        ct._eta_smoother = EtaSmoother(float(total))
        ct._eta_last_sample = 0.0
        self.nominal = nominal

    def sample(self, wall, completed, in_flight=0.0, phase='testing', blocked=False):
        """Take one sample and return the estimate now published, if any.

        blocked=True leaves the rate limiter's last-sample mark where it is, which is how
        a caller arriving too soon after the previous sample is simulated.
        """
        now = datetime.utcnow()
        s = ct._state
        s.run_started_at = now - timedelta(seconds=wall)
        s.completed_channels = completed
        s.current_phase = phase
        s.current_test_started_at = (now - timedelta(seconds=in_flight)
                                     if phase == 'testing' else None)
        if not blocked:
            # Far enough back that max(floor, nominal) has certainly elapsed.
            ct._eta_last_sample -= 10_000.0
        ct._advance_eta()
        return ct._state.eta_seconds


def tearDownModule():
    ct._state = ct.RunState()
    ct._eta_smoother = None
    ct._eta_last_sample = 0.0


class EtaSuppressionTests(unittest.TestCase):
    """When the estimate declines to say anything, and why."""

    def tearDown(self):
        tearDownModule()

    def test_no_estimate_for_a_single_channel_run(self):
        r = _Run(total=1)
        self.assertIsNone(r.sample(wall=120, completed=0, in_flight=60))

    def test_no_estimate_before_the_first_channel_finishes(self):
        # Plenty of wall time and a channel most of the way through, but nothing has
        # actually been measured yet - the only input so far is the configured cost.
        r = _Run(total=40)
        self.assertIsNone(r.sample(wall=30, completed=0, in_flight=30))

    def test_an_estimate_appears_once_a_channel_has_finished(self):
        r = _Run(total=40)
        r.sample(wall=35, completed=1)
        eta = r.sample(wall=70, completed=2)
        self.assertIsNotNone(eta)
        # 2 of 40 in 70s is 35s a channel, so ~38 channels x 35s left.
        self.assertGreater(eta, 600)

    def test_the_estimate_is_never_below_a_minute(self):
        # Every surface renders this with fmt_duration, whose smallest unit is the
        # minute: an unfloored 45s estimate reads "~0m".
        r = _Run(total=40, nominal=1.0)
        eta = None
        for i in range(1, 41):
            eta = r.sample(wall=float(i), completed=i)
        self.assertIsNotNone(eta)
        self.assertGreaterEqual(eta, 60)

    def test_status_carries_the_estimate(self):
        r = _Run(total=40)
        r.sample(wall=35, completed=1)
        r.sample(wall=70, completed=2)
        self.assertEqual(ct.get_status()['eta_seconds'], ct._state.eta_seconds)


class EtaStabilityTests(unittest.TestCase):
    """The no-yo-yo half: what the estimate is allowed to do between samples."""

    def tearDown(self):
        tearDownModule()

    def test_adjacent_samples_stay_within_the_clamp(self):
        # A 40-channel run at 35s a channel with one channel in the middle taking five
        # times as long. A naive remaining x average-so-far estimate lurches on that
        # channel; this one may not.
        r = _Run(total=40)
        wall = 0.0
        etas = []
        for i in range(1, 41):
            cost = 175.0 if i == 12 else 35.0
            # Mid-channel sample, then the completion sample.
            etas.append(r.sample(wall=wall + cost / 2, completed=i - 1, in_flight=cost / 2))
            wall += cost
            etas.append(r.sample(wall=wall, completed=i))
        seen = [e for e in etas if e is not None]
        self.assertTrue(seen, 'the estimate never appeared on a healthy run')
        for a, b in zip(seen, seen[1:]):
            if a >= 120:  # below that the human rounding buckets dominate
                self.assertLessEqual(abs(b - a) / a, 0.35,
                                     f'estimate swung {a}s -> {b}s between samples')

    def test_a_steady_run_walks_the_estimate_down(self):
        r = _Run(total=40)
        seen = []
        for i in range(1, 41):
            eta = r.sample(wall=35.0 * i, completed=i)
            if eta is not None:
                seen.append(eta)
        self.assertGreater(len(seen), 5)
        self.assertLess(seen[-1], seen[0],
                        'a run that is progressing normally must not end further out '
                        'than it started')

    def test_an_overrunning_channel_pushes_the_estimate_up(self):
        # The stall case. A channel that runs long must not be allowed to read as
        # several channels' worth of progress - that would walk the estimate DOWN while
        # the run is making no headway, which is the stopped-clock failure this display
        # is not allowed to have.
        r = _Run(total=40)
        for i in range(1, 11):
            r.sample(wall=35.0 * i, completed=i)
        settled = ct._state.eta_seconds
        self.assertIsNotNone(settled)
        seen = []
        for extra in (60.0, 120.0, 180.0, 240.0, 300.0):
            seen.append(r.sample(wall=350.0 + extra, completed=10, in_flight=extra))
        for a, b in zip(seen, seen[1:]):
            self.assertGreaterEqual(b, a, f'estimate fell {a}s -> {b}s during a stall')
        self.assertGreater(seen[-1], settled,
                           'a channel that overran left the estimate where it was')

    def test_progress_never_exceeds_the_channel_count(self):
        # The last channel's in-flight fraction must not push measured progress past the
        # total, which would make "remaining" negative.
        r = _Run(total=40)
        for i in range(1, 40):
            r.sample(wall=35.0 * i, completed=i)
        eta = r.sample(wall=35.0 * 40 + 500, completed=39, in_flight=535)
        self.assertIsNotNone(eta)
        self.assertGreaterEqual(eta, 0)


class EtaCadenceTests(unittest.TestCase):
    """Who is allowed to move the smoother, and how often."""

    def tearDown(self):
        tearDownModule()

    def test_reading_the_status_does_not_advance_the_estimate(self):
        # EtaSmoother carries state between samples, so a reader that advanced it would
        # make the number depend on how many browser tabs happen to be open.
        r = _Run(total=40)
        r.sample(wall=35, completed=1)
        r.sample(wall=70, completed=2)
        before = ct._state.eta_seconds
        mark = ct._eta_last_sample
        internals = (ct._eta_smoother._ewma_rate, ct._eta_smoother._prev_wall,
                     ct._eta_smoother._prev_out, ct._eta_smoother._last_eta)
        for _ in range(50):
            ct.get_status()
        self.assertEqual(ct._state.eta_seconds, before)
        self.assertEqual(ct._eta_last_sample, mark)
        self.assertEqual((ct._eta_smoother._ewma_rate, ct._eta_smoother._prev_wall,
                          ct._eta_smoother._prev_out, ct._eta_smoother._last_eta),
                         internals)

    def test_a_caller_arriving_too_soon_takes_no_sample(self):
        # The floor is what gives the smoother's clamp something to bound: at one sample
        # per channel "at most a fifth per update" means a fifth per channel, where at
        # one sample per poll it would mean nothing over that same span.
        r = _Run(total=40)
        r.sample(wall=35, completed=1)
        r.sample(wall=70, completed=2)
        before = ct._state.eta_seconds
        mark = ct._eta_last_sample
        prev_out = ct._eta_smoother._prev_out
        self.assertEqual(r.sample(wall=72, completed=2, in_flight=2, blocked=True), before)
        self.assertEqual(ct._eta_last_sample, mark)
        self.assertEqual(ct._eta_smoother._prev_out, prev_out)

    def test_a_finished_run_says_nothing(self):
        r = _Run(total=40)
        r.sample(wall=35, completed=1)
        r.sample(wall=70, completed=2)
        self.assertIsNotNone(ct._state.eta_seconds)
        with ct._lock:
            ct._state.clear()
        self.assertIsNone(ct.get_status()['eta_seconds'])

    def test_a_new_run_drops_the_previous_runs_smoother(self):
        # Carried over, the old smoother's +/-20% clamp would drag this run's first
        # estimates toward a rate measured on a different set of channels.
        r = _Run(total=40)
        for i in range(1, 11):
            r.sample(wall=35.0 * i, completed=i)
        stale = ct._eta_smoother
        self.assertIsNotNone(stale)
        with ct._lock:
            ct._reset_run_state()
        self.assertIsNone(ct._eta_smoother)
        self.assertIsNone(ct._state.eta_seconds)
        ct._end_run()


class EtaRunSetupTests(unittest.TestCase):
    """What the run loop hands the estimator when it starts."""

    def tearDown(self):
        tearDownModule()

    def test_the_loop_records_what_one_channel_is_expected_to_cost(self):
        with ct._lock:
            ct._state = ct.RunState(is_running=True)
        ct._run_channel_loop(None, [], wait_sec=5, test_duration_sec=30)
        self.assertEqual(ct._state.nominal_channel_seconds, 35.0)
        self.assertIsNotNone(ct._eta_smoother)

    def test_the_nominal_cost_falls_back_to_the_configured_default(self):
        with ct._lock:
            ct._state = ct.RunState(is_running=True)
        ct._run_channel_loop(None, [], wait_sec=5)
        self.assertGreater(ct._state.nominal_channel_seconds, 5.0)


class EtaOnThePagesTests(unittest.TestCase):
    """Both live surfaces, rendered. The estimate is computed once on the server and each
    page reads it, so a page that recomputed it - or one that dropped it in a refactor, as
    the original did - is what these catch."""

    def setUp(self):
        self.t = make_test_app()
        self.app = self.t.app
        self.client = self.app.test_client()

    def tearDown(self):
        # Before cleanup(), not after: its leak guard looks for a run still flagged
        # running, and the state here is a hand-built stand-in for one, with no thread
        # behind it for request_stop() to reach.
        tearDownModule()
        self.t.cleanup()

    def _live_run(self, eta_seconds):
        """Put a job's run on the clock with a published estimate, the way the Dashboard's
        health check card and the group page's hero both find it."""
        with self.app.app_context():
            acc = seed.make_account()
            chans = [seed.make_channel(acc, name=f'Ch {i}') for i in range(4)]
            job = seed.make_test_job(name='Nightly', channels=chans, status='RUNNING')
            from app import db
            db.session.commit()
            job_id = job.id
        with ct._lock:
            ct._state = ct.RunState(
                is_running=True, current_phase='testing', current_job_id=job_id,
                total_channels=4, completed_channels=1, nominal_channel_seconds=35.0,
                run_started_at=datetime.utcnow() - timedelta(seconds=40),
                eta_seconds=eta_seconds,
            )
        return job_id

    def test_the_dashboard_card_shows_the_estimate(self):
        self._live_run(eta_seconds=780)
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('id="hc-eta"', html)
        self.assertIn('~13m left', html)

    def test_the_dashboard_card_says_nothing_before_the_first_estimate(self):
        # Server-rendered initial state equals the nothing-measured state; the poll
        # upgrades it and is never needed to calm it down.
        self._live_run(eta_seconds=None)
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('id="hc-eta"', html)
        self.assertNotIn(' left</span>', html)

    def test_the_group_page_carries_the_hero_stat(self):
        self._live_run(eta_seconds=780)
        html = self.client.get('/channel-groups/1').get_data(as_text=True)
        self.assertIn('id="gd-hs-eta"', html)
        self.assertIn('Est. remaining', html)


if __name__ == '__main__':
    unittest.main()
