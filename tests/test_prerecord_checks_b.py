"""Tier 2 - Pre-record checks B: reactive failing-channel warnings.

Guards DESIGN-prerecord-checks.md §1-2 (changelog/237, "Pre-record checks B"): when a channel health check fails a channel - or, for a channel-kind group, when the
group's best-scoring member is failing - and that channel/group backs a SCHEDULED
recording, a standing WARN RECORDING_CHANNEL_FAILING alert is raised (deduped, keyed by a
stable source), and auto-dismissed on recovery or when the recording leaves SCHEDULED
(started, cancelled, deleted).

Covers, in order:
  - ChannelFailingReasonTests: the three-rule channel_failing_reason() helper (§1), pure
    logic against constructed rows.
  - DirectRecordingAlertTests: create/dedupe/dismiss for a channel-direct SCHEDULED
    recording, including the group_id-precedence fix - a group-backed recording also
    carries a frozen channel_id (the member picked at creation), so a naive
    channel_id-only query would wrongly apply the direct rule to it.
  - GroupRecordingAlertTests: the group-level rule (score of pick_best_member's pick,
    test=None always).
  - DismissOnLeavingScheduledTests: dismiss_recording_failing_alerts() in isolation.
  - LeavingScheduledWiringTests: the same dismissal driven through the REAL production
    call sites (recorder.start_recording/abort_recording, the cancel/delete routes) -
    proves the wiring, not just the helper.
  - RealRunWiringTests: drives the actual channel_tester.run_channel_test() path (no real
    ffmpeg - a fake Popen that exits instantly with no data) to prove
    assess_scheduled_recording_impact is really called after
    apply_test_health_observation inside _run_channel_test_inner.

No real ffmpeg, no network - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_prerecord_checks_b
"""
import copy
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, channel_tester, recorder  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app.database import Alert, Channel, ChannelTest  # noqa: E402
from app.health_score import (  # noqa: E402
    channel_failing_reason, assess_scheduled_recording_impact,
    dismiss_recording_failing_alerts,
)
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

ALERT_TYPE = 'RECORDING_CHANNEL_FAILING'


class FakeProc:
    """Stands in for a Popen'd ffmpeg that exits instantly with no data. Trimmed copy of
    tests/test_tester_preemption.py's FakeProc - duplicated here rather than imported so
    this file has no cross-test-module dependency."""

    def __init__(self):
        self._returncode = None
        self.stderr = None
        self.signals = []

    def send_signal(self, sig):
        # terminate_or_kill() continues a possibly-suspended child before terminating it
        # (dev/changelog/952), so a stand-in that cannot take a signal is not a faithful one.
        self.signals.append(sig)

    def poll(self):
        return self._returncode

    def terminate(self):
        self._returncode = -15

    def kill(self):
        self._returncode = -9

    def wait(self, timeout=None):
        self._returncode = self._returncode if self._returncode is not None else 0
        return self._returncode


def _active_failing_alerts():
    return Alert.query.filter_by(alert_type=ALERT_TYPE, dismissed_at=None).all()


class ChannelFailingReasonTests(unittest.TestCase):
    """§1's three rules, against real Channel/ChannelTest rows (ORM models, no I/O)."""

    def setUp(self):
        self.channel = Channel(account_id=None, stream_id=1, name='Reason Channel',
                               stream_url='http://example.test/live/1', health_score=80,
                               consecutive_test_failures=0)
        # `failing_band: poor` with the default cut points means "failing below 50"
        # (dev/changelog/771 replaced the raw failing_score_threshold with a band).
        self.cfg = {'channel_testing': {'failing_band': 'poor',
                                        'failing_streak_threshold': 3}}

    def test_hard_failure_names_error_detail(self):
        test = ChannelTest(channel_id=self.channel.id, status='FAILED',
                           error_detail='no data received', test_started_at=datetime.utcnow())
        reason = channel_failing_reason(test, self.channel, self.cfg)
        self.assertIsNotNone(reason)
        self.assertIn('no data received', reason)

    def test_cancelled_test_is_never_failing_evidence(self):
        test = ChannelTest(channel_id=self.channel.id, status='CANCELLED',
                           test_started_at=datetime.utcnow())
        # Channel score (80) is well above the Poor band's ceiling and a CANCELLED
        # test never touches it (score_test_quality excludes it from blending).
        self.assertIsNone(channel_failing_reason(test, self.channel, self.cfg))

    def test_completed_test_with_embedded_warn_is_not_itself_failing(self):
        test = ChannelTest(channel_id=self.channel.id, status='COMPLETED',
                           error_detail='Low bitrate: 400 kbps for 1280x720 (<=720p threshold: 1000 kbps)',
                           test_started_at=datetime.utcnow())
        self.assertIsNone(channel_failing_reason(test, self.channel, self.cfg))

    def test_score_below_threshold_is_failing_even_with_no_test(self):
        self.channel.health_score = 10
        reason = channel_failing_reason(None, self.channel, self.cfg)
        self.assertIsNotNone(reason)
        self.assertIn('10', reason)
        # Names the band, not a bare number the rest of the UI never mentions.
        self.assertIn('Poor', reason)

    def test_score_at_threshold_is_not_failing(self):
        # 50 is the Poor band's ceiling, i.e. the first Fair score.
        self.channel.health_score = 50
        self.assertIsNone(channel_failing_reason(None, self.channel, self.cfg))

    def test_failing_band_none_disables_the_score_rule(self):
        self.channel.health_score = 1
        cfg = {'channel_testing': {'failing_band': 'none'}}
        self.assertIsNone(channel_failing_reason(None, self.channel, cfg))

    def test_streak_at_threshold_is_failing_even_with_high_score(self):
        # The real-data case (dev/changelog/478): a channel with a good prior history
        # (score 80, a Good band well above the failing one) still hard-fails once its trailing
        # streak reaches the threshold - the score blend alone would miss this for weeks.
        self.channel.consecutive_test_failures = 3
        reason = channel_failing_reason(None, self.channel, self.cfg)
        self.assertIsNotNone(reason)
        self.assertIn('3', reason)
        self.assertIn('consecutive', reason)

    def test_streak_below_threshold_is_not_failing(self):
        self.channel.consecutive_test_failures = 2
        self.assertIsNone(channel_failing_reason(None, self.channel, self.cfg))

    def test_zero_streak_threshold_disables_the_streak_rule(self):
        self.channel.consecutive_test_failures = 99
        cfg = {'channel_testing': {'failing_band': 'poor', 'failing_streak_threshold': 0}}
        self.assertIsNone(channel_failing_reason(None, self.channel, cfg))

    def test_streak_rule_fires_regardless_of_which_test_is_passed(self):
        # Rule 2 is channel-level state, not test-dependent - it must fire the same way
        # whether test is None (schedule-time/group check) or a COMPLETED test object.
        self.channel.consecutive_test_failures = 5
        completed_test = ChannelTest(channel_id=self.channel.id, status='COMPLETED',
                                     test_started_at=datetime.utcnow())
        self.assertIsNotNone(channel_failing_reason(completed_test, self.channel, self.cfg))


class DirectRecordingAlertTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acct = seed.make_account()
        self.channel = seed.make_channel(self.acct, name='Direct Channel')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _make_test_row(self, status='FAILED', error_detail='no data received'):
        t = seed.make_channel_test(self.channel, all_null=False, status=status,
                                   error_detail=error_detail)
        db.session.commit()
        return t

    def test_failed_test_creates_one_alert_with_stable_source(self):
        rec = seed.make_recording(status='SCHEDULED', channel_id=self.channel.id)
        db.session.commit()
        test = self._make_test_row()

        assess_scheduled_recording_impact(self.t.app, test.id)

        alerts = _active_failing_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].source, f'recfail:rec:{rec.id}:ch:{self.channel.id}')
        self.assertEqual(alerts[0].recording_id, rec.id)

    def test_second_failed_test_does_not_duplicate(self):
        seed.make_recording(status='SCHEDULED', channel_id=self.channel.id)
        db.session.commit()
        t1 = self._make_test_row()
        assess_scheduled_recording_impact(self.t.app, t1.id)
        t2 = self._make_test_row()
        assess_scheduled_recording_impact(self.t.app, t2.id)

        self.assertEqual(len(_active_failing_alerts()), 1)

    def test_passing_test_dismisses_the_alert(self):
        seed.make_recording(status='SCHEDULED', channel_id=self.channel.id)
        db.session.commit()
        t1 = self._make_test_row()
        assess_scheduled_recording_impact(self.t.app, t1.id)
        self.assertEqual(len(_active_failing_alerts()), 1)

        ch = db.session.get(Channel, self.channel.id)
        ch.health_score = 90
        db.session.commit()
        t2 = self._make_test_row(status='COMPLETED', error_detail=None)
        assess_scheduled_recording_impact(self.t.app, t2.id)

        self.assertEqual(len(_active_failing_alerts()), 0)

    def test_cancelled_test_on_healthy_channel_fires_nothing(self):
        seed.make_recording(status='SCHEDULED', channel_id=self.channel.id)
        db.session.commit()
        t = self._make_test_row(status='CANCELLED',
                                error_detail='Interrupted by recording start')

        assess_scheduled_recording_impact(self.t.app, t.id)

        self.assertEqual(_active_failing_alerts(), [])

    def test_group_backed_recording_is_not_judged_as_direct(self):
        """The identity-precedence fix: a group-backed SCHEDULED recording also carries
        a frozen channel_id (the member picked at record creation). A FAILED test on
        that exact channel must NOT raise a direct-style alert for it - only the
        group-level rule (evaluated on the group's best member) may, and here the other
        member is healthy so nothing should fire at all."""
        other_member = seed.make_channel(self.acct, name='Healthy Member', health_score=90)
        group = seed.make_group(name='G', members=[self.channel, other_member])
        db.session.commit()
        seed.make_recording(status='SCHEDULED', channel_id=self.channel.id, group_id=group.id)
        db.session.commit()
        t = self._make_test_row()

        assess_scheduled_recording_impact(self.t.app, t.id)

        self.assertEqual(_active_failing_alerts(), [],
                         'a group-backed recording must be judged by the group rule, '
                         'not by one member\'s own hard failure')


class GroupRecordingAlertTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acct = seed.make_account()
        self.m1 = seed.make_channel(self.acct, name='Member A', health_score=90)
        self.m2 = seed.make_channel(self.acct, name='Member B', health_score=90)
        self.group = seed.make_group(name='Grp', members=[self.m1, self.m2])
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_all_members_healthy_fires_nothing(self):
        seed.make_recording(status='SCHEDULED', channel_id=self.m1.id, group_id=self.group.id)
        db.session.commit()
        test = seed.make_channel_test(self.m1, all_null=False, status='COMPLETED')
        db.session.commit()

        assess_scheduled_recording_impact(self.t.app, test.id)

        self.assertEqual(_active_failing_alerts(), [])

    def test_best_member_under_threshold_fires_the_group_source_alert(self):
        self.m1.health_score = 10
        self.m2.health_score = 5
        db.session.commit()
        rec = seed.make_recording(status='SCHEDULED', channel_id=self.m1.id, group_id=self.group.id)
        db.session.commit()
        test = seed.make_channel_test(self.m1, all_null=False, status='COMPLETED')
        db.session.commit()

        assess_scheduled_recording_impact(self.t.app, test.id)

        alerts = _active_failing_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].source, f'recfail:rec:{rec.id}:grp:{self.group.id}')

    def test_recovery_dismisses_the_group_alert(self):
        self.m1.health_score = 10
        self.m2.health_score = 5
        db.session.commit()
        seed.make_recording(status='SCHEDULED', channel_id=self.m1.id, group_id=self.group.id)
        db.session.commit()
        t1 = seed.make_channel_test(self.m1, all_null=False, status='COMPLETED')
        db.session.commit()
        assess_scheduled_recording_impact(self.t.app, t1.id)
        self.assertEqual(len(_active_failing_alerts()), 1)

        self.m1.health_score = 95
        db.session.commit()
        t2 = seed.make_channel_test(self.m1, all_null=False, status='COMPLETED')
        db.session.commit()
        assess_scheduled_recording_impact(self.t.app, t2.id)

        self.assertEqual(_active_failing_alerts(), [])


class ScheduleTimeAlertTests(unittest.TestCase):
    """Creation-time half of DESIGN-prerecord-checks.md §2 - closes the gap dev/changelog/478
    fixes: a recording scheduled directly onto an already-failing channel previously got no
    warning until the next scheduled test happened to run and hit
    assess_scheduled_recording_impact. Exercised through the real POST /recordings/new-json
    route (app/routes/recordings.py::new_recording_json), not evaluate_and_alert_recording
    called directly, to prove the wiring."""

    def setUp(self):
        # start_scheduler=True: new_recording_json's schedule_recording() call needs a
        # live APScheduler instance to register the start/stop jobs against. Every
        # start_time below is an hour in the future, so nothing actually fires during
        # the test (same reasoning as CreateRecordingContentionTests would need if it
        # weren't deliberately testing the immediate-fire path).
        self.t = make_test_app(start_scheduler=True)
        # This suite targets the alert-raising wiring, not CSRF (same as
        # CreateRecordingContentionTests in test_contention.py).
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acct = seed.make_account()
        self.channel = seed.make_channel(self.acct, name='Streaking Channel',
                                         health_score=80, consecutive_test_failures=3)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _post(self, **fields):
        start = datetime.utcnow() + timedelta(hours=1)
        stop = start + timedelta(hours=1)
        data = {
            'name': 'Test Rec',
            'url': 'http://example.test/live/1',
            'start_time': start.strftime('%Y-%m-%dT%H:%M'),
            'stop_time': stop.strftime('%Y-%m-%dT%H:%M'),
        }
        data.update(fields)
        return self.t.client.post('/recordings/new-json', data=data)

    def test_scheduling_directly_onto_a_streaking_channel_raises_the_alert(self):
        resp = self._post(channel_id=str(self.channel.id))
        self.assertEqual(resp.status_code, 200)
        rec_id = resp.get_json()['id']

        alerts = _active_failing_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].source, f'recfail:rec:{rec_id}:ch:{self.channel.id}')

    def test_scheduling_onto_a_healthy_channel_raises_nothing(self):
        healthy = seed.make_channel(self.acct, name='Healthy', health_score=90)
        db.session.commit()
        resp = self._post(channel_id=str(healthy.id))
        self.assertEqual(resp.status_code, 200)

        self.assertEqual(_active_failing_alerts(), [])

    def test_group_backed_creation_onto_streaking_best_member_raises_group_alert(self):
        other = seed.make_channel(self.acct, name='Also Streaking', health_score=70,
                                  consecutive_test_failures=5)
        group = seed.make_group(name='G', members=[self.channel, other])
        db.session.commit()
        resp = self._post(group_id=str(group.id))
        self.assertEqual(resp.status_code, 200)
        rec_id = resp.get_json()['id']

        alerts = _active_failing_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].source, f'recfail:rec:{rec_id}:grp:{group.id}')


class DismissOnLeavingScheduledTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acct = seed.make_account()
        self.channel = seed.make_channel(self.acct, name='Dismiss Channel')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_dismiss_clears_the_active_alert(self):
        rec = seed.make_recording(status='SCHEDULED', channel_id=self.channel.id)
        db.session.commit()
        alert = Alert(alert_type=ALERT_TYPE, severity='WARN', title='t',
                     source=f'recfail:rec:{rec.id}:ch:{self.channel.id}', recording_id=rec.id)
        db.session.add(alert)
        db.session.commit()

        dismiss_recording_failing_alerts(rec.id)

        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Alert, alert.id).dismissed_at)

    def test_dismiss_is_a_noop_when_nothing_active(self):
        rec = seed.make_recording(status='SCHEDULED', channel_id=self.channel.id)
        db.session.commit()
        dismiss_recording_failing_alerts(rec.id)  # must not raise


class LeavingScheduledWiringTests(unittest.TestCase):
    """The dismiss call driven through the real production transition points, not just
    dismiss_recording_failing_alerts() called directly. Needs the real jobstore
    (start_scheduler=True): abort/cancel/delete all route through
    scheduler.unschedule_recording(), which touches the live APScheduler instance
    (see tests/test_teardown.py for the same requirement)."""

    def setUp(self):
        connlim._holders.clear()
        self.t = make_test_app(start_scheduler=True)
        self.acct = seed.make_account()
        self.channel = seed.make_channel(self.acct, name='Wired Channel')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()
        connlim._holders.clear()

    def _seed_active_alert(self, rec):
        alert = Alert(alert_type=ALERT_TYPE, severity='WARN', title='t',
                     source=f'recfail:rec:{rec.id}:ch:{self.channel.id}', recording_id=rec.id)
        db.session.add(alert)
        db.session.commit()
        return alert

    def test_start_recording_dismisses(self):
        rec = seed.make_recording(
            status='SCHEDULED', channel_id=self.channel.id,
            start_time=datetime.utcnow() - timedelta(minutes=1),
            stop_time=datetime.utcnow() + timedelta(hours=1))
        db.session.commit()
        alert = self._seed_active_alert(rec)

        with mock.patch.object(recorder, '_launch_segment'):
            recorder.start_recording(self.t.app, rec.id)

        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Alert, alert.id).dismissed_at)

    def test_abort_recording_dismisses(self):
        rec = seed.make_recording(status='SCHEDULED', channel_id=self.channel.id)
        db.session.commit()
        alert = self._seed_active_alert(rec)

        recorder.abort_recording(self.t.app, rec.id)

        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Alert, alert.id).dismissed_at)

    def test_cancel_route_dismisses_a_scheduled_recording(self):
        rec = seed.make_recording(status='SCHEDULED', channel_id=self.channel.id)
        db.session.commit()
        alert = self._seed_active_alert(rec)
        self.t.app.config['WTF_CSRF_ENABLED'] = False

        resp = self.t.client.post(f'/recordings/{rec.id}/cancel')
        self.assertEqual(resp.status_code, 302)

        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Alert, alert.id).dismissed_at)

    def test_delete_route_dismisses(self):
        rec = seed.make_recording(status='SCHEDULED', channel_id=self.channel.id)
        db.session.commit()
        alert = self._seed_active_alert(rec)
        self.t.app.config['WTF_CSRF_ENABLED'] = False

        resp = self.t.client.post(f'/recordings/{rec.id}/delete')
        self.assertEqual(resp.status_code, 302)

        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Alert, alert.id).dismissed_at)


class RealRunWiringTests(unittest.TestCase):
    """Drives the actual channel_tester.run_channel_test() path (no real ffmpeg - a fake
    Popen that exits instantly with no data, connect_retries=0 so there's exactly one
    attempt and no retry sleep) to prove assess_scheduled_recording_impact is really
    wired in right after apply_test_health_observation inside _run_channel_test_inner -
    not just unit-tested against a hand-built ChannelTest row.

    connect_retries=0 has to reach the code as a patched app.config.load_config, not a
    make_test_app(extra_overrides=...) kwarg: _run_channel_test_inner calls load_config()
    itself at run time (CLAUDE.md §Testing's documented gotcha - extra_overrides is only
    visible to create_app(), never to a runtime load_config() call), so with the
    unpatched real config.yaml's connect_retries (2) this test would burn two real
    retry_delay sleeps for no reason."""

    def setUp(self):
        connlim._holders.clear()
        self.t = make_test_app()
        self.acct = seed.make_account()
        self.channel = seed.make_channel(self.acct, name='Wired Test Channel')
        db.session.commit()
        # A genuinely idle RunState, not _reset_run_state() - that helper is meant for
        # run-START (it sets is_running=True), so using it here/in tearDown would leave
        # _state.is_running stuck True for whatever test runs next in the process (this
        # test calls run_channel_test() directly, never through run_on_demand_test_job,
        # so nothing else ever clears it back to False).
        with channel_tester._lock:
            channel_tester._state = channel_tester.RunState()
        from app.config import _DEFAULTS
        self._cfg = copy.deepcopy(_DEFAULTS)
        self._cfg['channel_testing']['connect_retries'] = 0

    def tearDown(self):
        with channel_tester._lock:
            channel_tester._state = channel_tester.RunState()
        self.t.cleanup()
        connlim._holders.clear()

    def test_failed_run_raises_the_alert_through_the_real_hook(self):
        rec = seed.make_recording(status='SCHEDULED', channel_id=self.channel.id)
        db.session.commit()

        def _popen(*a, **kw):
            proc = FakeProc()
            proc._returncode = 1
            return proc

        with mock.patch('app.config.load_config', return_value=self._cfg), \
             mock.patch.object(channel_tester.subprocess, 'Popen', _popen), \
             mock.patch.object(channel_tester, '_drain_stderr', lambda *a, **kw: None), \
             mock.patch.object(channel_tester, 'wait_for_file_data', lambda *a, **kw: False):
            channel_tester.run_channel_test(self.t.app, self.channel.id)

        row = (ChannelTest.query.filter_by(channel_id=self.channel.id)
               .order_by(ChannelTest.id.desc()).first())
        self.assertEqual(row.status, 'FAILED')

        alerts = _active_failing_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].source, f'recfail:rec:{rec.id}:ch:{self.channel.id}')


if __name__ == '__main__':
    unittest.main()
