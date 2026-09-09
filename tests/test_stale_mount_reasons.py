"""A storage path that is UNREACHABLE must not be reported as missing, colliding, or fine.

Guards dev/docs/BUGS.md 2026-08-18 @ 10:00:00 AM ET. `/dvr` on this box is a CIFS mount
that has twice answered ESTALE on its own root while paths through it still resolved,
and every stdlib convenience predicate hides that: os.path.isdir()/exists() catch every
OSError and answer False, and os.makedirs(exist_ok=True) re-raises the mkdir's EEXIST
because its own isdir() recheck was the thing that got lied to. Both behaviors were
verified against Python 3.12 on this machine (dev/changelog/723) before the fix was
written; the errnos are simulated here rather than provoked, because neither condition
reproduces from a clean checkout on a healthy mount.

Four surfaces told the user something false:

  * The move-on-complete step's FILE_MOVED event read "Move FAILED: [Errno 17] File
    exists", sending the reader hunting a filename collision that does not exist.
  * _disk_bytes() walked up to the nearest ancestor os.path.exists() admitted to, which
    on a stale mount root is `/` - so the sidebar and the Maintenance meter reported the
    ROOT filesystem's free space under the /dvr label. Principle 1: a number the user
    cannot explain is worse than no number.
  * start_recording marked the recording FAILED with "DVR output directory /dvr does not
    exist" and sent the operator off to mkdir a directory that was already there.
  * The startup check logged that same wrong reason plus a `sudo mkdir -p` instruction
    that cannot help.

Every file this touches is written under the test's own tmpdir - never the real /dvr.
  python3 -m unittest tests.test_stale_mount_reasons
"""
import errno
import os
import unittest
from unittest import mock

from app import db
from app.database import (Recording, RecordingEvent, RECORDING_FAILED,
                          REC_STATUS_FAILED, REC_STATUS_SCHEDULED)
from app.fs_utils import (DirProbe, PATH_DENIED, PATH_MISSING, PATH_NOT_A_DIR, PATH_OK,
                          PATH_UNREACHABLE, PathUnusableError, classify_oserror,
                          describe_dir_problem, ensure_dir, log_dir_outcome_change,
                          probe_dir)
from tests.support import seed
from tests.support.app import make_test_app


def _raise_for(paths, err=errno.ESTALE):
    """A drop-in for a path-taking syscall that fails with `err` for `paths` only.

    Anything else is delegated to the real function, so patching os.stat globally does
    not take the interpreter down with it.
    """
    def wrapper(real):
        def fake(path, *a, **kw):
            if str(path) in paths:
                raise OSError(err, os.strerror(err), str(path))
            return real(path, *a, **kw)
        return fake
    return wrapper


class ProbeClassificationTests(unittest.TestCase):
    """probe_dir names the condition; it never collapses two of them into one answer."""

    def setUp(self):
        self.t = make_test_app()
        self.dir = os.path.join(self.t._tmpdir, 'storage')
        os.makedirs(self.dir, exist_ok=True)

    def tearDown(self):
        self.t.cleanup()

    def test_an_existing_directory_is_ok(self):
        self.assertEqual(PATH_OK, probe_dir(self.dir).outcome)

    def test_an_absent_path_is_missing_not_unreachable(self):
        probe = probe_dir(os.path.join(self.dir, 'nope'))
        self.assertEqual(PATH_MISSING, probe.outcome)

    def test_a_stale_handle_is_unreachable_not_missing(self):
        """The whole point: ESTALE is not evidence the path is gone."""
        with mock.patch('os.stat', _raise_for({self.dir})(os.stat)):
            probe = probe_dir(self.dir)
        self.assertEqual(PATH_UNREACHABLE, probe.outcome)
        self.assertEqual(errno.ESTALE, probe.errno)

    def test_every_storage_layer_errno_reads_as_unreachable(self):
        for name in ('ESTALE', 'EIO', 'ENOTCONN', 'EHOSTDOWN', 'ETIMEDOUT', 'EREMOTEIO'):
            code = getattr(errno, name)
            with self.subTest(errno=name):
                probe = classify_oserror(OSError(code, os.strerror(code), self.dir))
                self.assertEqual(PATH_UNREACHABLE, probe.outcome)

    def test_a_permission_error_is_its_own_outcome(self):
        probe = classify_oserror(OSError(errno.EACCES, os.strerror(errno.EACCES), self.dir))
        self.assertEqual(PATH_DENIED, probe.outcome)

    def test_a_file_where_a_directory_belongs_is_its_own_outcome(self):
        path = os.path.join(self.dir, 'a-file')
        with open(path, 'w') as fh:
            fh.write('x')
        self.assertEqual(PATH_NOT_A_DIR, probe_dir(path).outcome)

    def test_an_unclassified_errno_falls_to_unreachable_never_to_missing(self):
        """Guessing "missing" would invite a caller to create or overwrite a path whose
        real state we do not know. Unreachable is the side that refuses."""
        probe = classify_oserror(OSError(errno.EBADF, os.strerror(errno.EBADF), self.dir))
        self.assertEqual(PATH_UNREACHABLE, probe.outcome)

    def test_every_outcome_has_a_sentence_that_names_it(self):
        for outcome, must_contain in (
            (PATH_MISSING, 'does not exist'),
            (PATH_NOT_A_DIR, 'not a directory'),
            (PATH_DENIED, 'not accessible'),
            (PATH_UNREACHABLE, 'not reachable'),
        ):
            with self.subTest(outcome=outcome):
                text = describe_dir_problem('/mnt/x', DirProbe(outcome, None, 'Stale file handle'))
                self.assertIn('/mnt/x', text)
                self.assertIn(must_contain, text)

    def test_the_unreachable_sentence_does_not_claim_the_data_is_gone(self):
        text = describe_dir_problem('/dvr', DirProbe(PATH_UNREACHABLE, errno.ESTALE,
                                                     'Stale file handle'))
        self.assertNotIn('does not exist', text)
        self.assertIn('Stale file handle', text)


class EnsureDirTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.dir = os.path.join(self.t._tmpdir, 'storage')
        os.makedirs(self.dir, exist_ok=True)

    def tearDown(self):
        self.t.cleanup()

    def test_creates_a_genuinely_missing_directory(self):
        target = os.path.join(self.dir, 'made', 'here')
        self.assertEqual(PATH_OK, ensure_dir(target).outcome)
        self.assertTrue(os.path.isdir(target))

    def test_an_existing_directory_is_accepted_without_a_mkdir(self):
        with mock.patch('os.makedirs', side_effect=AssertionError('must not be called')):
            self.assertEqual(PATH_OK, ensure_dir(self.dir).outcome)

    def test_a_stale_destination_raises_a_reason_not_a_file_exists_error(self):
        """The defect: makedirs(exist_ok=True) turns ESTALE into FileExistsError(17)."""
        with mock.patch('os.stat', _raise_for({self.dir})(os.stat)):
            with self.assertRaises(PathUnusableError) as caught:
                ensure_dir(self.dir)
        message = str(caught.exception)
        self.assertIn('not reachable', message)
        self.assertNotIn('File exists', message)
        self.assertNotIn('Errno 17', message)


class MoveDestinationReasonTests(unittest.TestCase):
    """The FILE_MOVED event names the dead mount, not a phantom filename collision."""

    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        self.dest = os.path.join(self.t._tmpdir, 'dvr-complete')
        os.makedirs(self.dvr, exist_ok=True)
        os.makedirs(self.dest, exist_ok=True)
        self.ts_path = os.path.join(self.dvr, 'show.ts')
        with open(self.ts_path, 'wb') as fh:
            fh.write(b'\0' * 2048)
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            'gather_health_data': False,
            'serialize_concat': False,
            'live_thumbnail': {'enabled': False},
            'post_process': {'enabled': False},
            'post_script': {'enabled': False},
            'move_on_complete': {'enabled': True, 'destination': self.dest},
        }})

    def tearDown(self):
        self.t.cleanup()

    def _run_move(self, stale_paths):
        rec = seed.make_recording(status='CONCATENATING', name='show')
        db.session.commit()
        rid = rec.id
        from app.postprocessor import do_postprocess
        with mock.patch('os.stat', _raise_for(stale_paths)(os.stat)):
            do_postprocess(self.t.app, rid, self.ts_path)
        db.session.expire_all()
        return rid, [e.detail for e in RecordingEvent.query.filter_by(
            recording_id=rid, event_type='FILE_MOVED').all()]

    def test_a_stale_destination_is_named_as_unreachable(self):
        _rid, details = self._run_move({self.dest})
        self.assertEqual(1, len(details), f'expected one FILE_MOVED event, got {details}')
        self.assertIn('not reachable', details[0])

    def test_the_event_does_not_report_a_filename_collision(self):
        _rid, details = self._run_move({self.dest})
        self.assertNotIn('File exists', details[0])
        self.assertNotIn('Errno 17', details[0])

    def test_the_file_is_still_named_so_the_user_can_find_it(self):
        _rid, details = self._run_move({self.dest})
        self.assertIn(self.ts_path, details[0],
                      'the event must still say where the recording actually is')

    def test_a_healthy_destination_still_moves(self):
        _rid, details = self._run_move(set())
        self.assertTrue(os.path.exists(os.path.join(self.dest, 'show.ts')),
                        f'move did not happen: {details}')
        self.assertNotIn('FAILED', details[0])


class DiskReadoutTests(unittest.TestCase):
    """_disk_bytes never answers with a different filesystem's numbers."""

    def setUp(self):
        self.t = make_test_app()
        self.mount = os.path.join(self.t._tmpdir, 'mnt')
        os.makedirs(self.mount, exist_ok=True)

    def tearDown(self):
        self.t.cleanup()

    def test_a_healthy_path_reports_its_filesystem(self):
        from app.routes.system import _disk_bytes
        total, free = _disk_bytes(self.mount)
        self.assertIsNotNone(total)
        self.assertGreater(total, 0)

    def test_an_uncreated_subdir_still_reports_its_parents_filesystem(self):
        """The ancestor walk exists for this case and must survive the fix."""
        from app.routes.system import _disk_bytes
        total, _free = _disk_bytes(os.path.join(self.mount, 'not-made-yet'))
        self.assertIsNotNone(total)

    def test_a_stale_mount_reports_no_data_rather_than_the_parents_numbers(self):
        from app.routes.system import _disk_bytes
        root_total = os.statvfs('/').f_blocks * os.statvfs('/').f_frsize
        with mock.patch('os.stat', _raise_for({self.mount})(os.stat)):
            total, free = _disk_bytes(self.mount)
        self.assertIsNone(total, f'reported {total} for an unreachable mount')
        self.assertIsNone(free)
        self.assertNotEqual(root_total, total)

    def test_a_stale_mount_under_a_requested_subdir_also_stops_the_walk(self):
        """The configured path is usually a subdir of the mount, so the walk has to stop
        at the mount root rather than sailing past it to /."""
        from app.routes.system import _disk_bytes
        with mock.patch('os.stat', _raise_for({self.mount})(os.stat)):
            total, _free = _disk_bytes(os.path.join(self.mount, 'recordings'))
        self.assertIsNone(total)

    def test_the_sidebar_payload_omits_the_disk_rather_than_lying(self):
        from app.routes.system import _system_stats_dict
        self.t.sandbox_config({'recording': {'dvr_output_dir': self.mount}})
        with mock.patch('os.stat', _raise_for({self.mount})(os.stat)):
            stats = _system_stats_dict()
        self.assertIsNone(stats['disk_dvr'])


class OutcomeLoggingTests(unittest.TestCase):
    """The polling disk readout logs the transition, not every poll."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _probe(self, outcome):
        return DirProbe(outcome, errno.ESTALE if outcome == PATH_UNREACHABLE else None,
                        'Stale file handle')

    def test_the_first_bad_outcome_warns(self):
        with self.assertLogs('app.fs_utils', level='WARNING') as caught:
            log_dir_outcome_change('/mnt/a', self._probe(PATH_UNREACHABLE), 'Disk usage for')
        self.assertIn('not reachable', caught.output[0])

    def test_an_unchanged_outcome_does_not_warn_again(self):
        log_dir_outcome_change('/mnt/b', self._probe(PATH_UNREACHABLE), 'Disk usage for')
        with mock.patch('app.fs_utils.log') as logger:
            log_dir_outcome_change('/mnt/b', self._probe(PATH_UNREACHABLE), 'Disk usage for')
        self.assertFalse(logger.warning.called,
                         'a 15s poll must not restate the same warning forever')

    def test_recovery_is_announced(self):
        log_dir_outcome_change('/mnt/c', self._probe(PATH_UNREACHABLE), 'Disk usage for')
        with self.assertLogs('app.fs_utils', level='INFO') as caught:
            log_dir_outcome_change('/mnt/c', self._probe(PATH_OK), 'Disk usage for')
        self.assertIn('reachable again', caught.output[0])

    def test_a_path_that_was_never_bad_is_silent_when_it_is_fine(self):
        with mock.patch('app.fs_utils.log') as logger:
            log_dir_outcome_change('/mnt/d', self._probe(PATH_OK), 'Disk usage for')
        self.assertFalse(logger.info.called)
        self.assertFalse(logger.warning.called)


class StartRecordingReasonTests(unittest.TestCase):
    """A recording refused for storage reasons says which storage reason."""

    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            'live_thumbnail': {'enabled': False},
            'post_process': {'enabled': False},
        }})

    def tearDown(self):
        self.t.cleanup()

    def _start_with_dvr_stale(self):
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, name='stale-mount')
        db.session.commit()
        rid = rec.id
        from app.recorder import start_recording
        with mock.patch('os.stat', _raise_for({self.dvr})(os.stat)):
            start_recording(self.t.app, rid)
        db.session.expire_all()
        return rid

    def test_the_recording_still_fails_visibly(self):
        rid = self._start_with_dvr_stale()
        self.assertEqual(REC_STATUS_FAILED, db.session.get(Recording, rid).status)

    def test_the_failure_event_names_the_mount_not_a_missing_directory(self):
        rid = self._start_with_dvr_stale()
        details = [e.detail for e in RecordingEvent.query.filter_by(
            recording_id=rid, event_type=RECORDING_FAILED).all()]
        self.assertEqual(1, len(details), f'expected one RECORDING_FAILED event, got {details}')
        self.assertIn('not reachable', details[0])
        self.assertNotIn('does not exist', details[0])

    def test_a_genuinely_missing_dir_still_says_so(self):
        """The fix must not blur the two conditions in the other direction either."""
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, name='no-dir')
        db.session.commit()
        rid = rec.id
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': os.path.join(self.t._tmpdir, 'never-made'),
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            'live_thumbnail': {'enabled': False},
            'post_process': {'enabled': False},
        }})
        from app.recorder import start_recording
        start_recording(self.t.app, rid)
        db.session.expire_all()
        details = [e.detail for e in RecordingEvent.query.filter_by(
            recording_id=rid, event_type=RECORDING_FAILED).all()]
        self.assertIn('does not exist', details[0])


class StartupCheckTests(unittest.TestCase):
    """The startup warning does not tell the operator to mkdir a directory that exists."""

    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)

    def tearDown(self):
        self.t.cleanup()

    def test_a_stale_mount_is_not_advertised_as_a_mkdir_problem(self):
        from app import _ensure_dvr_dir
        cfg = {'recording': {'dvr_output_dir': self.dvr}}
        with mock.patch('os.stat', _raise_for({self.dvr})(os.stat)):
            with self.assertLogs(level='WARNING') as caught:
                _ensure_dvr_dir(cfg)
        joined = '\n'.join(caught.output)
        self.assertIn('not reachable', joined)
        self.assertNotIn('mkdir', joined)

    def test_a_missing_dir_still_gets_the_mkdir_advice(self):
        from app import _ensure_dvr_dir
        cfg = {'recording': {'dvr_output_dir': os.path.join(self.t._tmpdir, 'never-made')}}
        with self.assertLogs(level='WARNING') as caught:
            _ensure_dvr_dir(cfg)
        self.assertIn('mkdir', '\n'.join(caught.output))

    def test_a_healthy_dir_is_silent(self):
        from app import _ensure_dvr_dir
        with mock.patch('logging.warning') as warn:
            _ensure_dvr_dir({'recording': {'dvr_output_dir': self.dvr}})
        self.assertFalse(warn.called)


if __name__ == '__main__':
    unittest.main()
