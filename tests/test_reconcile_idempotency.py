"""DB-backed idempotency test for the channel-group reconcile log/alert layer
(app/channel_groups.py::_log_and_alert_reconcile).

This is the half of the engine the pure suite (test_channel_groups.py) can't reach:
`_log_and_alert_reconcile` uses the CHANNEL_GROUP_FORMAT_* ChannelGroupEvent log itself as
durable state to stay idempotent, so it needs a real DB. It pins the 2026-07-17 Part E
BUGS.md invariant: an outlier gains exactly one MISMATCH event (and an open alert);
re-running with no format/reference change creates nothing new (idempotent); a member
that ceases to be an outlier gains a RESOLVED event and its open alert is dismissed.

CrossGroupStateTests pins the 2026-08-22 BUGS.md entry: that state is per-membership, so
two groups sharing a member must not overwrite each other's verdict.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 tests/test_reconcile_idempotency.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.channel_groups import plan_reconcile, _log_and_alert_reconcile  # noqa: E402
from app.database import (  # noqa: E402
    Channel, ChannelGroup, ChannelGroupMember, ChannelEvent, ChannelGroupEvent, Alert,
    CHANNEL_GROUP_FORMAT_MISMATCH, CHANNEL_GROUP_FORMAT_RESOLVED,
    GROUP_FORMAT_HIGHEST_SCORE, GROUP_FORMAT_HEALTH_CHECK_ONLY,
)
from tests.support import make_test_app  # noqa: E402

HD = ('1920x1080', 60)
SD = ('1280x720', 30)


class FakeTest:
    """status is part of the fake, not decoration: only a COMPLETED check measures a
    format (app/channel_groups.py::format_key), so a fake without one reads as untested
    and every outlier assertion below would pass for the wrong reason."""

    def __init__(self, key, status='COMPLETED'):
        self.resolution, self.fps = key
        self.status = status


class ReconcileIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

        # highest_score, not the health_check_only default: DESIGN-channel-groups-model.md
        # 16 gates every format warning on the group being a recording source, so a
        # default-strategy group deliberately logs nothing at all (see
        # FormatWarningGateTests below).
        self.group = ChannelGroup(name='Test Group',
                                  format_strategy=GROUP_FORMAT_HIGHEST_SCORE)
        db.session.add(self.group)
        db.session.flush()
        # reference member (HD, best score) + one SD outlier
        self.ref_ch = Channel(account_id=1, stream_id=1, name='HD feed',
                              stream_url='u1', health_score=100)
        self.outlier = Channel(account_id=1, stream_id=2, name='SD feed',
                              stream_url='u2', health_score=80)
        db.session.add_all([self.ref_ch, self.outlier])
        db.session.flush()
        db.session.add_all([
            # recording_enabled: the format reference is derived from the members the
            # group would actually record from, so a group with none has no reference
            # and nothing to be an outlier of.
            ChannelGroupMember(group_id=self.group.id, channel_id=self.ref_ch.id,
                               position=0, recording_enabled=True),
            ChannelGroupMember(group_id=self.group.id, channel_id=self.outlier.id,
                               position=1, recording_enabled=True),
        ])
        db.session.commit()
        self.members = [self.ref_ch, self.outlier]
        self.memberships = list(self.group.memberships)

    def tearDown(self):
        self.t.cleanup()

    def _counts(self):
        return (
            ChannelGroupEvent.query.filter_by(
                group_id=self.group.id, event_type=CHANNEL_GROUP_FORMAT_MISMATCH).count(),
            ChannelGroupEvent.query.filter_by(
                group_id=self.group.id, event_type=CHANNEL_GROUP_FORMAT_RESOLVED).count(),
            Alert.query.filter_by(alert_type='GROUP_FORMAT_MISMATCH').count(),
        )

    def _reconcile(self, latest):
        diff = plan_reconcile(self.group, self.memberships, latest)
        _log_and_alert_reconcile(self.group, self.members, latest, diff)

    def test_mismatch_then_idempotent_then_resolved(self):
        mixed = {self.ref_ch.id: FakeTest(HD), self.outlier.id: FakeTest(SD)}

        # ── Run 1: the SD member is a fresh outlier → one MISMATCH event logged.
        self._reconcile(mixed)
        mm1, res1, al1 = self._counts()
        self.assertEqual(mm1, 1, 'exactly one MISMATCH event for the fresh outlier')
        self.assertEqual(res1, 0)
        # The MISMATCH event is attributed to the outlier, not the reference member, and
        # is scoped to the group that raised it.
        ev = ChannelGroupEvent.query.filter_by(
            event_type=CHANNEL_GROUP_FORMAT_MISMATCH).one()
        self.assertEqual(ev.channel_id, self.outlier.id)
        self.assertEqual(ev.group_id, self.group.id)

        # ── Run 2: identical state → NOTHING new (the idempotency guarantee).
        self._reconcile(mixed)
        mm2, res2, al2 = self._counts()
        self.assertEqual((mm2, res2, al2), (mm1, res1, al1),
                         'a second reconcile with no change must add no events/alerts')

        # ── Run 3: the outlier now conforms (reports HD) → one RESOLVED event, and
        # any open mismatch alert for it is dismissed.
        conformed = {self.ref_ch.id: FakeTest(HD), self.outlier.id: FakeTest(HD)}
        self._reconcile(conformed)
        mm3, res3, _ = self._counts()
        self.assertEqual(mm3, mm1, 'no new MISMATCH events on resolve')
        self.assertEqual(res3, 1, 'exactly one RESOLVED event when the outlier conforms')
        open_alerts = Alert.query.filter(
            Alert.alert_type == 'GROUP_FORMAT_MISMATCH',
            Alert.source == f'group:{self.group.id}:ch:{self.outlier.id}',
            Alert.dismissed_at.is_(None)).count()
        self.assertEqual(open_alerts, 0, 'the resolved member has no open mismatch alert')

        # ── Run 4: resolved state re-run → still idempotent (no new RESOLVED spam).
        self._reconcile(conformed)
        mm4, res4, _ = self._counts()
        self.assertEqual((mm4, res4), (mm3, res3),
                         'a second reconcile after resolve must add nothing')

    def test_format_events_are_not_written_to_channel_events(self):
        """The state is per-membership, so it may not live on the per-channel table.

        BUGS.md 2026-08-22: channel_events has no group_id, so a row there cannot say
        WHICH group found the mismatch - which is what let two groups overwrite one
        state slot.
        """
        self._reconcile({self.ref_ch.id: FakeTest(HD), self.outlier.id: FakeTest(SD)})
        self.assertEqual(
            ChannelEvent.query.filter(ChannelEvent.event_type.in_(
                [CHANNEL_GROUP_FORMAT_MISMATCH, CHANNEL_GROUP_FORMAT_RESOLVED])).count(),
            0, 'format events belong on ChannelGroupEvent, never on ChannelEvent')


class CrossGroupStateTests(unittest.TestCase):
    """BUGS.md 2026-08-22: a member shared by two groups flapped forever.

    Group A (a recording source, reference HD) calls its SD member an outlier. Group B
    holds the same channel. While the transition state lived on ChannelEvent - keyed on
    channel_id with no group_id - B's reconcile read A's MISMATCH as its own previous
    state, logged RESOLVED against it, and A read that back as "no longer mismatched" on
    its next pass. Every health check produced one MISMATCH/RESOLVED pair per shared
    member, and the alert dismissal never matched because its source key was correctly
    group-scoped while the state driving it was not.
    """

    def setUp(self):
        self.t = make_test_app()
        self.ref_ch = Channel(account_id=1, stream_id=1, name='HD feed',
                              stream_url='u1', health_score=100)
        self.shared = Channel(account_id=1, stream_id=2, name='SD feed',
                              stream_url='u2', health_score=80)
        db.session.add_all([self.ref_ch, self.shared])
        db.session.flush()

        # Group A: a recording source whose reference is HD, so `shared` is an outlier.
        self.a = ChannelGroup(name='A', format_strategy=GROUP_FORMAT_HIGHEST_SCORE)
        # Group B: also a recording source, but its only recording-enabled member IS the
        # shared channel, so its reference is SD and `shared` conforms. Two groups, two
        # honest and opposite verdicts about one channel - which is exactly the case a
        # per-channel state slot cannot represent.
        self.b = ChannelGroup(name='B', format_strategy=GROUP_FORMAT_HIGHEST_SCORE)
        db.session.add_all([self.a, self.b])
        db.session.flush()
        db.session.add_all([
            ChannelGroupMember(group_id=self.a.id, channel_id=self.ref_ch.id,
                               position=0, recording_enabled=True),
            ChannelGroupMember(group_id=self.a.id, channel_id=self.shared.id,
                               position=1, recording_enabled=True),
            ChannelGroupMember(group_id=self.b.id, channel_id=self.shared.id,
                               position=0, recording_enabled=True),
        ])
        db.session.commit()
        self.latest = {self.ref_ch.id: FakeTest(HD), self.shared.id: FakeTest(SD)}

    def tearDown(self):
        self.t.cleanup()

    def _reconcile(self, group):
        memberships = list(group.memberships)
        members = [m.channel for m in memberships]
        diff = plan_reconcile(group, memberships, self.latest)
        _log_and_alert_reconcile(group, members, self.latest, diff)

    def _counts(self, group):
        return (
            ChannelGroupEvent.query.filter_by(
                group_id=group.id, event_type=CHANNEL_GROUP_FORMAT_MISMATCH).count(),
            ChannelGroupEvent.query.filter_by(
                group_id=group.id, event_type=CHANNEL_GROUP_FORMAT_RESOLVED).count(),
        )

    def test_two_groups_sharing_a_member_do_not_flap(self):
        # One test on the shared channel reconciles every group it belongs to, back to
        # back - app/channel_tester.py::_recheck_group_format for a one-off test, and
        # _settle_group_formats once at the end of a health check run (dev/changelog/934).
        for _ in range(3):
            self._reconcile(self.a)
            self._reconcile(self.b)

        self.assertEqual(self._counts(self.a), (1, 0),
                         'group A logs its outlier exactly once, however often B runs')
        self.assertEqual(self._counts(self.b), (0, 0),
                         'group B, where the member conforms, logs nothing at all')

    def test_reconciling_raises_no_alerts_at_all(self):
        """The user-visible half: 214 undismissed duplicates had accumulated in the wild,
        and on the live database all 124 unread warnings were this one type. Since
        dev/changelog/928 a member differing from its group's format is shown on the group -
        the banner, the member's pill and the events above - and raises nothing.

        (The per-group event assertions in the sibling test are what now carry "group B's
        pass does not clear group A's state", which the standing alert used to also prove.)
        """
        for _ in range(5):
            self._reconcile(self.a)
            self._reconcile(self.b)
        self.assertEqual(
            Alert.query.filter_by(alert_type='GROUP_FORMAT_MISMATCH').count(), 0,
            'a format mismatch is shown on the group, never raised as an alert')


class FormatWarningGateTests(unittest.TestCase):
    """DESIGN-channel-groups-model.md 16: every format warning is gated on the group
    being a recording source (format_strategy != health_check_only). Part E was not,
    which is how a health-check-only group came to have opinions about formats."""

    def setUp(self):
        self.t = make_test_app()
        self.ref_ch = Channel(account_id=1, stream_id=1, name='HD feed',
                              stream_url='u1', health_score=100)
        self.outlier = Channel(account_id=1, stream_id=2, name='SD feed',
                               stream_url='u2', health_score=80)
        db.session.add_all([self.ref_ch, self.outlier])
        db.session.flush()
        self.group = ChannelGroup(name='Checks only',
                                  format_strategy=GROUP_FORMAT_HEALTH_CHECK_ONLY)
        db.session.add(self.group)
        db.session.flush()
        db.session.add_all([
            ChannelGroupMember(group_id=self.group.id, channel_id=self.ref_ch.id,
                               position=0, recording_enabled=True),
            ChannelGroupMember(group_id=self.group.id, channel_id=self.outlier.id,
                               position=1, recording_enabled=True),
        ])
        db.session.commit()
        self.latest = {self.ref_ch.id: FakeTest(HD), self.outlier.id: FakeTest(SD)}

    def tearDown(self):
        self.t.cleanup()

    def _reconcile(self):
        memberships = list(self.group.memberships)
        members = [m.channel for m in memberships]
        diff = plan_reconcile(self.group, memberships, self.latest)
        _log_and_alert_reconcile(self.group, members, self.latest, diff)

    def test_health_check_only_group_logs_no_format_events_or_alerts(self):
        self._reconcile()
        self.assertEqual(
            ChannelGroupEvent.query.filter(ChannelGroupEvent.event_type.in_(
                [CHANNEL_GROUP_FORMAT_MISMATCH, CHANNEL_GROUP_FORMAT_RESOLVED])).count(),
            0, 'a group that is not a recording source has no format to be wrong about')
        self.assertEqual(
            Alert.query.filter_by(alert_type='GROUP_FORMAT_MISMATCH').count(), 0)

    def test_switching_a_group_off_recording_clears_a_straggler_alert(self):
        """Teardown releases what the create path acquired: the warning stopped applying,
        so it clears rather than stranding an alert nothing can ever dismiss.

        Nothing raises GROUP_FORMAT_MISMATCH any more (dev/changelog/928), so the row is
        seeded here the way an install upgrading from an older build would still hold one.
        The dismissing half deliberately stayed behind: without it a pre-928 row would
        outlive every code path able to clear it.
        """
        from datetime import datetime
        db.session.add(Alert(
            alert_type='GROUP_FORMAT_MISMATCH', severity='WARN',
            title='Format mismatch in group "Checks only"',
            source=f'group:{self.group.id}:ch:{self.outlier.id}',
            created_at=datetime.utcnow()))
        db.session.commit()
        self.assertEqual(
            Alert.query.filter(Alert.alert_type == 'GROUP_FORMAT_MISMATCH',
                               Alert.dismissed_at.is_(None)).count(), 1)

        self.group.format_strategy = GROUP_FORMAT_HEALTH_CHECK_ONLY
        db.session.commit()
        self._reconcile()
        self.assertEqual(
            Alert.query.filter(Alert.alert_type == 'GROUP_FORMAT_MISMATCH',
                               Alert.dismissed_at.is_(None)).count(), 0,
            'a straggler mismatch alert is dismissed when the group stops recording')


if __name__ == '__main__':
    unittest.main(verbosity=2)
