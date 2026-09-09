"""A configured storage directory that stops answering reaches the Alert Center.

dev/changelog/723 taught four surfaces to NAME an unreachable mount instead of
misreporting it, but deliberately added no surface: a dead `/dvr` produced one WARNING
in dvr.log and nothing else, while silently disabling recording entirely. These guard
the standing alert added on top of it (dev/changelog/868) and the two properties that
make it safe to raise from a hot path: it is one row per path, not one row per poll (the
sidebar's disk readout runs every 15s per open browser tab), and it clears itself when
the path answers again.

Also guards the move-on-complete destination, which was worse off than the DVR
directory: its probe sat behind `os.path.exists(dest)`, the error-swallowing predicate
app/fs_utils.py exists to replace, so a stale destination was skipped without even a log
line.

ESTALE is simulated rather than provoked - neither condition reproduces from a clean
checkout on a healthy mount. Every directory is under the test's own tmpdir, never /dvr.
  python3 -m unittest tests.test_storage_path_alert
"""
import errno
import os
import unittest
from unittest import mock

from app import fs_utils
from app.alerts import STORAGE_PATH_UNUSABLE
from app.database import Alert
from tests.support.app import make_test_app


def _stale_stat(paths):
    """os.stat that answers ESTALE for `paths` and delegates everything else.

    Patching os.stat globally without the delegation takes the interpreter down with it.
    """
    real = os.stat

    def fake(path, *a, **kw):
        if str(path) in paths:
            raise OSError(errno.ESTALE, os.strerror(errno.ESTALE), str(path))
        return real(path, *a, **kw)
    return fake


class StoragePathAlertTests(unittest.TestCase):
    """The standing STORAGE_PATH_UNUSABLE alert: raised on the way down, cleared on the
    way back up, and never stacked by the poll that observes it."""

    def setUp(self):
        self.t = make_test_app()
        self.mount = os.path.join(self.t._tmpdir, 'mnt')
        os.makedirs(self.mount, exist_ok=True)
        self.t.sandbox_config({'recording': {'dvr_output_dir': self.mount}})

    def tearDown(self):
        self.t.cleanup()

    def _poll(self, stale=()):
        from app.routes.system import _system_stats_dict
        if not stale:
            return _system_stats_dict()
        with mock.patch('os.stat', _stale_stat(set(stale))):
            return _system_stats_dict()

    def _alerts(self, source=None):
        q = Alert.query.filter_by(alert_type=STORAGE_PATH_UNUSABLE)
        if source is not None:
            q = q.filter_by(source=source)
        return q.all()

    def test_an_unreachable_dvr_dir_raises_an_alert(self):
        """The whole point: the condition reaches a surface, not only dvr.log."""
        self._poll(stale=[self.mount])
        rows = self._alerts()
        self.assertEqual(1, len(rows), 'a dead DVR directory raised no alert')
        self.assertEqual(self.mount, rows[0].source,
                         'the alert must be keyed on the path so two directories can '
                         'fail and recover independently')
        self.assertEqual('ERROR', rows[0].severity)
        self.assertIn('DVR output directory', rows[0].title)
        self.assertIn('not reachable', rows[0].body)
        self.assertIn(self.mount, rows[0].body)

    def test_a_repeat_poll_does_not_stack_a_second_row(self):
        """_disk_bytes runs every 15s per open browser tab; one condition is one row."""
        self._poll(stale=[self.mount])
        self._poll(stale=[self.mount])
        self._poll(stale=[self.mount])
        self.assertEqual(1, len(self._alerts()))

    def test_recovery_dismisses_the_standing_alert(self):
        self._poll(stale=[self.mount])
        self._poll()
        rows = self._alerts()
        self.assertEqual(1, len(rows))
        self.assertIsNotNone(rows[0].dismissed_at,
                             'the alert must clear itself once the path answers again')

    def test_a_fresh_failure_after_recovery_raises_a_new_alert(self):
        """Dismissing is not the same as suppressing: the mount going away twice is two
        events, and the second one must be visible."""
        self._poll(stale=[self.mount])
        self._poll()
        self._poll(stale=[self.mount])
        rows = self._alerts()
        self.assertEqual(2, len(rows))
        self.assertEqual(1, len([r for r in rows if r.dismissed_at is None]))

    def test_a_restart_does_not_duplicate_a_standing_alert(self):
        """The transition memory is per-process; the alert row is not. A restart while
        the mount is still down must not raise a second row for the same condition."""
        self._poll(stale=[self.mount])
        fs_utils._last_logged_outcome.clear()
        self._poll(stale=[self.mount])
        self.assertEqual(1, len(self._alerts()))

    def test_a_healthy_path_raises_nothing(self):
        self._poll()
        self.assertEqual([], self._alerts())


class MoveDestinationAlertTests(unittest.TestCase):
    """The move-on-complete destination is a configured storage path too, and used not
    to be probed at all when it was the thing that had gone away."""

    def setUp(self):
        self.t = make_test_app()
        self.mount = os.path.join(self.t._tmpdir, 'mnt')
        self.dest = os.path.join(self.t._tmpdir, 'archive')
        os.makedirs(self.mount, exist_ok=True)
        os.makedirs(self.dest, exist_ok=True)
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.mount,
            'move_on_complete': {'enabled': True, 'destination': self.dest},
        }})

    def tearDown(self):
        self.t.cleanup()

    def _poll(self, stale=()):
        from app.routes.system import _system_stats_dict
        with mock.patch('os.stat', _stale_stat(set(stale))):
            return _system_stats_dict()

    def test_an_unreachable_destination_raises_its_own_alert(self):
        """It sat behind os.path.exists(dest), which answers False for a stale mount, so
        the destination was skipped entirely - no meter, no log line, no alert."""
        stats = self._poll(stale=[self.dest])
        rows = Alert.query.filter_by(alert_type=STORAGE_PATH_UNUSABLE).all()
        self.assertEqual(1, len(rows))
        self.assertEqual(self.dest, rows[0].source)
        self.assertIn('Move-on-complete destination', rows[0].title)
        self.assertIsNone(stats['disk_complete'],
                          'no honest free-space answer is available for a stale mount')

    def test_the_two_paths_fail_independently(self):
        """Keyed on the path, so a dead destination says nothing about the DVR
        directory and recovering one does not clear the other."""
        self._poll(stale=[self.mount, self.dest])
        self.assertEqual(2, len(Alert.query.filter_by(
            alert_type=STORAGE_PATH_UNUSABLE, dismissed_at=None).all()))
        self._poll(stale=[self.dest])
        open_rows = Alert.query.filter_by(
            alert_type=STORAGE_PATH_UNUSABLE, dismissed_at=None).all()
        self.assertEqual([self.dest], [r.source for r in open_rows])


if __name__ == '__main__':
    unittest.main()
