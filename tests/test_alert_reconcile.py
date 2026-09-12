"""Tier 2 - an open self-clearing alert whose condition is over is reconciled at startup.

Guards dev/docs/BUGS.md 2026-09-12 @ 12:20:00 PM ET.

CONVERSION_FAILED and RECORDING_MOVE_FAILED are dismissed inline by do_postprocess, one call
AFTER the commit that records the success. That ordering has two live consequences:

  * A row raised before the inline clear existed (dev/changelog/930, 2026-09-12) was never
    cleared by anything, and never will be - nothing re-runs a conversion that already
    produced its mp4. One such alert stood over a recording that had been COMPLETED for
    nineteen days.
  * A process death in the gap between the two commits - `restart.sh --force` during a
    conversion is the ordinary way there - strands a new row the same way.

Both types are self_clearing, so a stranded row sits under the Alerts page's "Active alerts"
card, which offers no Dismiss at all (dev/changelog/932). The only way out was Ignore, which
suppresses every future alert of the class.

reconcile_failure_alerts() derives each row's liveness from what the recording itself
recorded, at every startup, rather than from whether one call happened to run.

No network, no real ffmpeg (CLAUDE.md §Testing).

Run standalone:
  python3 -m unittest tests.test_alert_reconcile
"""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.database import Alert  # noqa: E402
from app.postprocessor import reconcile_failure_alerts  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

MOVE_DEST = '/dvr-complete'
#: Written before the reconcile runs, so a row it leaves alone is provably untouched rather
#: than merely still open.
T_DISMISSED = datetime(2026, 9, 1, 12, 0)


class _ReconcileCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        # reconcile_failure_alerts() imports load_config INSIDE the function, so the patch
        # reaches it. make_test_app's extra_overrides would not: they are used once when the
        # app is built and never stored, so a runtime load_config() reads the real
        # config.yaml - where this machine's move destination happens to be the same path,
        # which would make the move cases pass for the wrong reason (CLAUDE.md §Testing).
        self.cfg = {'recording': {'post_process': {
            'move': {'enabled': True, 'destination': MOVE_DEST}}}}
        patcher = mock.patch('app.config.load_config', return_value=self.cfg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.t.cleanup()

    def _alert(self, alert_type, recording_id=None, **kw):
        a = Alert(alert_type=alert_type, severity='ERROR',
                  title=f'{alert_type} title', body='why',
                  source='postprocessor', recording_id=recording_id, **kw)
        db.session.add(a)
        db.session.commit()
        return a

    def _open(self, alert_id):
        return db.session.get(Alert, alert_id).dismissed_at is None


class ConversionReconcileTests(_ReconcileCase):
    def test_a_completed_recording_clears_its_stranded_conversion_alert(self):
        rec = seed.make_recording(status='COMPLETED', name='nascar',
                                  output_path=f'{MOVE_DEST}/nascar.mp4')
        a = self._alert('CONVERSION_FAILED', recording_id=rec.id)
        self.assertEqual(reconcile_failure_alerts(), 1)
        self.assertFalse(self._open(a.id))

    def test_a_recording_still_failed_keeps_its_alert(self):
        """The whole point of the card is that a live problem cannot be hidden, so the
        reconcile must only ever act on a positive observation of recovery."""
        rec = seed.make_recording(status='FAILED', name='still broken')
        a = self._alert('CONVERSION_FAILED', recording_id=rec.id)
        self.assertEqual(reconcile_failure_alerts(), 0)
        self.assertTrue(self._open(a.id))

    def test_a_converting_recording_keeps_its_alert(self):
        """Startup relaunches a CONVERTING row before the reconcile runs; the attempt has
        not succeeded yet, so nothing has been observed to recover."""
        rec = seed.make_recording(status='CONVERTING', name='mid flight')
        a = self._alert('CONVERSION_FAILED', recording_id=rec.id)
        self.assertEqual(reconcile_failure_alerts(), 0)
        self.assertTrue(self._open(a.id))


class MoveReconcileTests(_ReconcileCase):
    def test_a_file_sitting_in_the_destination_clears_the_move_alert(self):
        rec = seed.make_recording(status='COMPLETED', name='moved',
                                  output_path=f'{MOVE_DEST}/moved.mp4')
        a = self._alert('RECORDING_MOVE_FAILED', recording_id=rec.id)
        self.assertEqual(reconcile_failure_alerts(), 1)
        self.assertFalse(self._open(a.id))

    def test_a_file_still_where_the_move_left_it_keeps_the_alert(self):
        """A failed move leaves output_path alone and the successful one rewrites it, so the
        column answers this directly - COMPLETED does not, since a move failure never
        changes the recording's status."""
        rec = seed.make_recording(status='COMPLETED', name='not moved',
                                  output_path='/dvr/not_moved.mp4')
        a = self._alert('RECORDING_MOVE_FAILED', recording_id=rec.id)
        self.assertEqual(reconcile_failure_alerts(), 0)
        self.assertTrue(self._open(a.id))

    def test_move_turned_off_is_not_evidence_either_way(self):
        """Turning the move off leaves its destination sitting in config.yaml, so the
        `enabled` flag is the half that has to be read - a file under a path nothing is
        moving to any more says nothing about a move that failed while it was on."""
        self.cfg['recording']['post_process']['move'] = {'enabled': False,
                                                         'destination': MOVE_DEST}
        rec = seed.make_recording(status='COMPLETED', name='move off',
                                  output_path=f'{MOVE_DEST}/move_off.mp4')
        a = self._alert('RECORDING_MOVE_FAILED', recording_id=rec.id)
        self.assertEqual(reconcile_failure_alerts(), 0)
        self.assertTrue(self._open(a.id))

    def test_a_trailing_slash_on_the_destination_still_matches(self):
        """The destination is a user-typed config string; a path that differs only in its
        spelling must not read as a different directory."""
        self.cfg['recording']['post_process']['move']['destination'] = f'{MOVE_DEST}/'
        rec = seed.make_recording(status='COMPLETED', name='slashed',
                                  output_path=f'{MOVE_DEST}/slashed.mp4')
        a = self._alert('RECORDING_MOVE_FAILED', recording_id=rec.id)
        self.assertEqual(reconcile_failure_alerts(), 1)
        self.assertFalse(self._open(a.id))


class ScopeTests(_ReconcileCase):
    def test_concatenation_failed_is_left_alone_over_a_completed_recording(self):
        """Deliberately not self-clearing (dev/changelog/930): nothing re-runs a concat that
        found nothing to concatenate, so the row is a record of a loss. A reconcile that
        swept every failure type by status would delete that record."""
        rec = seed.make_recording(status='COMPLETED', name='salvaged',
                                  output_path=f'{MOVE_DEST}/salvaged.mp4')
        a = self._alert('CONCATENATION_FAILED', recording_id=rec.id)
        self.assertEqual(reconcile_failure_alerts(), 0)
        self.assertTrue(self._open(a.id))

    def test_an_alert_naming_no_recording_is_left_alone(self):
        self._alert('CONVERSION_FAILED', recording_id=None)
        self.assertEqual(reconcile_failure_alerts(), 0)

    def test_an_alert_naming_a_recording_that_is_gone_is_left_alone(self):
        """Deleting a recording already dismisses and unlinks its alerts
        (dev/changelog/929), so a dangling id is not an observation of recovery."""
        a = self._alert('CONVERSION_FAILED', recording_id=999999)
        self.assertEqual(reconcile_failure_alerts(), 0)
        self.assertTrue(self._open(a.id))

    def test_an_already_dismissed_row_keeps_its_original_timestamp(self):
        """Anchors only move forward (CLAUDE.md time accounting): re-stamping a row that was
        dismissed weeks ago would report the reconcile's own run as when it was cleared."""
        rec = seed.make_recording(status='COMPLETED', name='old news',
                                  output_path=f'{MOVE_DEST}/old.mp4')
        a = self._alert('CONVERSION_FAILED', recording_id=rec.id, dismissed_at=T_DISMISSED)
        self.assertEqual(reconcile_failure_alerts(), 0)
        self.assertEqual(db.session.get(Alert, a.id).dismissed_at, T_DISMISSED)

    def test_several_rows_are_cleared_in_one_pass(self):
        rows = []
        for i in range(3):
            rec = seed.make_recording(status='COMPLETED', name=f'rec {i}',
                                      output_path=f'{MOVE_DEST}/rec{i}.mp4')
            rows.append(self._alert('CONVERSION_FAILED', recording_id=rec.id).id)
        self.assertEqual(reconcile_failure_alerts(), 3)
        self.assertTrue(all(not self._open(rid) for rid in rows))


class StartupWiringTests(unittest.TestCase):
    """The reconcile is only worth anything if something runs it. A helper nobody calls is
    the shape this whole defect took: the inline clear existed and simply never ran."""

    def setUp(self):
        # resume_in_progress_recordings() sweeps orphaned APScheduler on-demand jobs at the
        # end, which needs a live scheduler.
        self.t = make_test_app(start_scheduler=True)

    def tearDown(self):
        self.t.cleanup()

    def test_startup_recovery_reconciles_the_failure_alerts(self):
        from app.scheduler import resume_in_progress_recordings

        with mock.patch('app.postprocessor.reconcile_failure_alerts') as fake:
            resume_in_progress_recordings(self.t.app)
        self.assertTrue(fake.called,
                        'resume_in_progress_recordings must reconcile the post-processing '
                        'failure alerts, or a stranded row is never revisited')


if __name__ == '__main__':
    unittest.main()
