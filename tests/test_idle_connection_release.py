"""A background thread hands its database connection back before it goes idle.

Guards dev/docs/BUGS.md 2026-11-14 @ 06:14:41 pm ET. A session keeps the connection its last
query checked out until the transaction ends, so every wait loop in the recording chain held
one of the background pool's connections while it slept, and do_postprocess's own app
context left the calling concat chain holding a second for all of post-processing. Eight
conversions waiting their turn behind back-to-back recordings took all twelve of prod's
background connections, and fourteen recordings could not start.

Each test reads `db.session().in_transaction()` at the moment the thread would sit idle: a
session in a transaction is a session holding a connection.

No real ffmpeg and no network: sleeps are patched, subprocess.Popen is faked where a
conversion "runs".
  python3 -m unittest tests.test_idle_connection_release
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.concatenator as concatmod  # noqa: E402
import app.config as cfgmod  # noqa: E402
import app.postprocessor as ppmod  # noqa: E402
import app.proc_utils as pumod  # noqa: E402
from app import db  # noqa: E402
from app.database import REC_STATUS_IN_PROGRESS, Recording  # noqa: E402
from app.db_utils import release_idle_connection  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.fakes import FakePopen  # noqa: E402


def _set_status(app, recording_id, status):
    """Write from a context of its own, as a request or another chain would."""
    with app.app_context():
        r = db.session.get(Recording, recording_id)
        r.status = status
        db.session.commit()


class ReleaseIdleConnectionTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.rid = seed.make_recording(status='SCHEDULED', name='r').id
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_ends_a_read_transaction(self):
        db.session.get(Recording, self.rid).name
        self.assertTrue(db.session().in_transaction())
        self.assertTrue(release_idle_connection())
        self.assertFalse(db.session().in_transaction())

    def test_keeps_an_unsaved_change_and_its_connection(self):
        """A pending change is the caller's unfinished write; a rollback would lose it."""
        db.session.get(Recording, self.rid).name = 'renamed'
        with self.assertLogs('app.db_utils', 'WARNING'):
            self.assertFalse(release_idle_connection())
        self.assertTrue(db.session().in_transaction())
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rid).name, 'renamed')


class WaitLoopTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_conversion_wait_holds_no_connection_while_it_sleeps(self):
        conv_id = seed.make_recording(status='ANALYZING', name='conv').id
        blocker_id = seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='blocker').id
        db.session.commit()
        held = []

        def _sleep(_seconds):
            held.append(db.session().in_transaction())
            if len(held) == 2:
                _set_status(self.t.app, blocker_id, 'COMPLETED')

        with mock.patch.object(ppmod.time, 'sleep', side_effect=_sleep):
            self.assertEqual(ppmod._wait_for_conversion_clear(conv_id, 0, poll_seconds=0), 'clear')
        self.assertEqual(held, [False, False])

    def test_concat_wait_holds_no_connection_while_it_sleeps(self):
        rid = seed.make_recording(status='CONCATENATING', name='joining').id
        blocker_id = seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='blocker').id
        db.session.commit()
        held = []

        def _sleep(_seconds):
            held.append(db.session().in_transaction())
            if len(held) == 2:
                _set_status(self.t.app, blocker_id, 'COMPLETED')

        with mock.patch.object(concatmod.time, 'sleep', side_effect=_sleep):
            concatmod._wait_for_no_active_recording(rid, poll_seconds=0)
        self.assertEqual(held, [False, False])


class PostprocessHandoffTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_the_callers_connection_is_released_before_post_processing(self):
        """do_postprocess works in a context, and so a session, of its own; the caller's
        session is the concat chain's, which has nothing left to do until it returns."""
        rid = seed.make_recording(status='ANALYZING', name='r').id
        db.session.commit()
        db.session.get(Recording, rid).name
        caller = db.session()
        self.assertTrue(caller.in_transaction())
        held = []
        real = cfgmod.load_config

        def _load_config(*a, **kw):
            held.append(caller.in_transaction())
            return real(*a, **kw)

        # An id with no row: do_postprocess loads its settings, finds nothing and returns.
        with mock.patch.object(cfgmod, 'load_config', side_effect=_load_config):
            ppmod.do_postprocess(self.t.app, rid + 1000, '/nonexistent.ts')
        self.assertEqual(held[:1], [False])


class SupervisedConversionPollTests(unittest.TestCase):
    """The collision check runs every poll for the length of an encode, paused or not."""

    def setUp(self):
        self.t = make_test_app()
        self.rid = seed.make_recording(status='CONVERTING', name='conv').id
        db.session.commit()
        self.out = os.path.join(self.t._tmpdir, f'out_{self.rid}.mkv')

    def tearDown(self):
        with ppmod._active_lock:
            ppmod._active_conversions.clear()
        self.t.cleanup()

    def _held_between_polls(self, fake):
        held = []

        def _progress(_path):
            held.append(db.session().in_transaction())
            return (5_000_000, 1000, False)

        with mock.patch.object(pumod.subprocess, 'Popen', return_value=fake), \
             mock.patch.object(pumod, 'read_progress_tail', side_effect=_progress):
            ppmod.run_conversion_supervised(
                self.t.app, self.rid, ['/usr/bin/ffmpeg', '-i', 'src.ts', '-y', self.out],
                self.out, expected_duration=100, pre_output_timeout=30, interval=0.01,
                stall_seconds=0, collision_policy='cancel')
        return held

    def test_running_conversion_holds_no_connection_between_polls(self):
        held = self._held_between_polls(FakePopen(alive_ticks=4, returncode=0))
        self.assertGreaterEqual(len(held), 3)
        self.assertNotIn(True, held[1:])

    def test_paused_conversion_holds_no_connection_between_polls(self):
        seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='blocker')
        db.session.commit()
        fake = FakePopen(alive_ticks=5)
        held = self._held_between_polls(fake)
        self.assertTrue(fake.signals, 'the conversion never paused')
        self.assertGreaterEqual(len(held), 3)
        self.assertNotIn(True, held[1:])


if __name__ == '__main__':
    unittest.main()
