"""Stalls on two or more accounts of one provider inside five minutes are one outage, said
once (app/provider_stalls.py, dev/changelog/1476): one WARN alert per provider, refreshed
while the window keeps filling, and one PROVIDER_STALLS_CORRELATED event per recording in the
window. One account's stalls, a provider with one account, a channel on no provider and
stalls further apart than the window all stay silent. The watchdog calls it after every
stall's commit and goes on if it fails.

No network, no real ffmpeg: the watchdog case runs the shared rig (tests/support/watchdog.py)
over a local stand-in process. Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_provider_stalls
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import account_links as links, db, provider_stalls as ps  # noqa: E402
from app import watchdog as watchdog_mod  # noqa: E402
from app.database import (PROVIDER_STALLS_CORRELATED, STALL_DETECTED, Alert, Recording,  # noqa: E402
                          RecordingEvent, RecordingSegment)
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.watchdog import WatchdogHarness  # noqa: E402

T0 = datetime(2026, 11, 8, 12, 0, 0)


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.a1 = seed.make_account(name='Alpha panel')
        self.a2 = seed.make_account(name='Bravo panel')
        self.a3 = seed.make_account(name='Unrelated')
        db.session.commit()
        self.pid = links.create_provider('Skyline', [self.a1.id, self.a2.id])[0].id
        self.c1 = seed.make_channel(self.a1, name='USA on Alpha')
        self.c2 = seed.make_channel(self.a2, name='USA on Bravo')
        self.c3 = seed.make_channel(self.a3, name='USA elsewhere')
        self.r1 = seed.make_recording(status='IN_PROGRESS', name='Race', channel_id=self.c1.id)
        self.r2 = seed.make_recording(status='IN_PROGRESS', name='Game', channel_id=self.c2.id)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def alerts(self):
        return Alert.query.filter_by(alert_type=PROVIDER_STALLS_CORRELATED).all()

    def events(self, rid):
        return RecordingEvent.query.filter_by(recording_id=rid,
                                              event_type=PROVIDER_STALLS_CORRELATED).all()


class CorrelationTests(_Base):
    def test_two_accounts_inside_the_window_make_one_alert_and_an_event_each(self):
        self.assertFalse(ps.note_stall(self.r1.id, self.c1.id, now=T0))
        self.assertEqual(self.alerts(), [])
        self.assertTrue(ps.note_stall(self.r2.id, self.c2.id, now=T0 + timedelta(minutes=2)))
        rows = self.alerts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source, f'provider:{self.pid}:stalls')
        self.assertEqual(rows[0].severity, 'WARN')
        self.assertIn('Skyline', rows[0].title)
        self.assertIn('"Race"', rows[0].body)
        self.assertIn('"Game"', rows[0].body)
        self.assertIn('Alpha panel', rows[0].body)
        for rid in (self.r1.id, self.r2.id):
            evs = self.events(rid)
            self.assertEqual(len(evs), 1, rid)
            self.assertIn('2 accounts of Skyline', evs[0].detail)
            self.assertEqual(json.loads(evs[0].extra_data)['account_ids'],
                             sorted([self.a1.id, self.a2.id]))

    def test_a_third_stall_in_the_window_refreshes_the_alert_and_adds_no_second_event(self):
        ps.note_stall(self.r1.id, self.c1.id, now=T0)
        ps.note_stall(self.r2.id, self.c2.id, now=T0 + timedelta(minutes=1))
        first = self.alerts()[0]
        first_id = first.id
        # Mark it read and backdate it, so a refresh is visible as a change and not a tautology.
        first.read_at = datetime.utcnow()
        first.created_at = datetime.utcnow() - timedelta(hours=1)
        db.session.commit()
        self.assertTrue(ps.note_stall(self.r1.id, self.c1.id, now=T0 + timedelta(minutes=3)))
        db.session.expire_all()
        rows = self.alerts()
        self.assertEqual([r.id for r in rows], [first_id])          # refreshed, not stacked
        self.assertIsNone(rows[0].read_at)
        self.assertGreater(rows[0].created_at, datetime.utcnow() - timedelta(minutes=5))
        self.assertEqual(len(self.events(self.r1.id)), 1)
        self.assertEqual(len(self.events(self.r2.id)), 1)

    def test_a_dismissed_alert_is_raised_again_by_the_next_qualifying_stall(self):
        ps.note_stall(self.r1.id, self.c1.id, now=T0)
        ps.note_stall(self.r2.id, self.c2.id, now=T0 + timedelta(minutes=1))
        row = self.alerts()[0]
        row.dismissed_at = datetime.utcnow()
        db.session.commit()
        ps.note_stall(self.r1.id, self.c1.id, now=T0 + timedelta(minutes=2))
        self.assertEqual(len(self.alerts()), 2)


class SilenceTests(_Base):
    def test_one_accounts_stalls_do_not_qualify_however_many_recordings(self):
        other = seed.make_recording(status='IN_PROGRESS', name='Also on Alpha', channel_id=self.c1.id)
        db.session.commit()
        self.assertFalse(ps.note_stall(self.r1.id, self.c1.id, now=T0))
        self.assertFalse(ps.note_stall(other.id, self.c1.id, now=T0 + timedelta(minutes=1)))
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.events(self.r1.id), [])

    def test_stalls_further_apart_than_the_window_do_not_qualify(self):
        ps.note_stall(self.r1.id, self.c1.id, now=T0)
        self.assertFalse(ps.note_stall(self.r2.id, self.c2.id,
                                       now=T0 + ps.WINDOW + timedelta(seconds=1)))
        self.assertEqual(self.alerts(), [])

    def test_a_channel_on_no_provider_and_a_missing_channel_cost_nothing(self):
        r3 = seed.make_recording(status='IN_PROGRESS', channel_id=self.c3.id)
        db.session.commit()
        self.assertFalse(ps.note_stall(r3.id, self.c3.id, now=T0))
        self.assertFalse(ps.note_stall(r3.id, None, now=T0))
        self.assertFalse(ps.note_stall(r3.id, 999999, now=T0))
        self.assertEqual(ps._recent, {})

    def test_the_account_activity_card_shows_the_providers_alert_on_each_member(self):
        ps.note_stall(self.r1.id, self.c1.id, now=T0)
        ps.note_stall(self.r2.id, self.c2.id, now=T0 + timedelta(minutes=1))
        row = self.alerts()[0]
        row.created_at = datetime.utcnow()
        db.session.commit()
        for acc in (self.a1, self.a2):
            html = self.t.client.get(f'/accounts/{acc.id}').get_data(as_text=True)
            self.assertIn('Several accounts on Skyline stalled together', html, acc.name)
        html = self.t.client.get(f'/accounts/{self.a3.id}').get_data(as_text=True)
        self.assertNotIn('Several accounts on Skyline', html)


class WatchdogHookTests(WatchdogHarness):
    """The watchdog tells provider_stalls about every stall, with the segment's channel, and
    a failure inside the correlation never reaches the capture."""

    def _on_channel(self):
        acct = seed.make_account(name='Hooked')
        ch = seed.make_channel(acct, name='Hooked channel')
        seg = RecordingSegment.query.filter_by(recording_id=self.rid, segment_number=0).one()
        seg.channel_id = ch.id
        rec = db.session.get(Recording, self.rid)
        rec.channel_id = ch.id
        db.session.commit()
        return ch.id

    def test_a_stall_is_noted_with_the_segments_channel(self):
        cid = self._on_channel()
        self._exited_capture()
        with mock.patch.object(watchdog_mod.provider_stalls, 'note_stall',
                               return_value=False) as noted:
            self._run_until_segment_closed(stall_timeout=30)
        self.assertEqual([c.args for c in noted.call_args_list], [(self.rid, cid)])

    def test_a_failure_in_the_correlation_does_not_stop_the_restart(self):
        self._on_channel()
        self._exited_capture()
        with mock.patch.object(watchdog_mod.provider_stalls, 'note_stall',
                               side_effect=RuntimeError('boom')):
            seg, _elapsed = self._run_until_segment_closed(stall_timeout=30)
        self.assertIsNotNone(seg.ended_at)
        self.assertIsNotNone(self._stall_event(), 'the stall was still logged')
        self.assertEqual(self.launched, [1], 'the restart still happened')
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=self.rid, event_type=STALL_DETECTED).count(), 1)


if __name__ == '__main__':
    unittest.main()
