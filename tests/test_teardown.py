"""Tier 2 - teardown completeness (dev/changelog/268, chunk 5, defect class M).

Deleting/aborting a recording must release everything the create/start path acquired:
APScheduler start+stop jobs, segment + thumbnail files on disk, and (via ORM cascade)
segment/event rows. Guards the "one teardown path missed one artifact" family - most
concretely the 2026-07-16 09:12 AM bug where cancel_recording_json's copy of the
file-deletion loop omitted the live thumbnail.

The classes from LaunchFailureGiveUpReleasesEverythingTests down cover the two terminal
paths the user never triggers - the app giving up on a recording as FAILED, and the
service shutting down mid-capture - enumerated against what start_recording/_launch_segment
actually acquire: an APScheduler job pair, a connection slot, the _active entry, the ffmpeg
child, the watchdog thread, the segment's stderr spool, and the segment rows and files
(dev/changelog/731).

Needs the real jobstore (start_scheduler=True) to assert scheduler jobs are gone.

No network and no provider host: every child spawned here is `sys.executable -c ...`, a
local argv with no URL in it, which tests/support/netguard.py permits. Files are written
under make_test_app's temp dir, never /dvr.
"""
import glob
import os
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app import recorder  # noqa: E402
import app.config as cfgmod  # noqa: E402
from app.database import (  # noqa: E402
    RECORDING_FAILED, Recording, RecordingSegment, RecordingEvent, Alert, ChannelTest,
)
from app import scheduler as sched_mod  # noqa: E402
from app.scheduler import schedule_recording, get_scheduler  # noqa: E402
from app.watchdog import WatchdogThread  # noqa: E402
# The two rigs that already know how to fake a live capture cheaply, subclassed rather
# than copied. Neither base class carries tests of its own, so importing them here does
# not re-run anything.
from tests.test_launch_failure_retry import _LaunchRetryTestCase  # noqa: E402
from tests.test_watchdog_connection_release import _ConnectionReleaseHarness  # noqa: E402
from tests.test_watchdog_process_exit import _sleeper  # noqa: E402


class DeleteScheduledTeardownTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_delete_scheduled_removes_jobs_and_cascades_rows(self):
        # Future times so the date-trigger jobs stay registered (a past run_date fires
        # and self-removes immediately).
        future = datetime.utcnow() + timedelta(days=3650)
        rec = seed.make_recording(status='SCHEDULED', channel_id=self.ch.id,
                                  start_time=future, stop_time=future + timedelta(hours=1),
                                  with_events=True, with_segment=True)
        db.session.commit()
        rid = rec.id
        schedule_recording(self.t.app, rid, rec.start_time, rec.stop_time)
        sched = get_scheduler()
        self.assertIsNotNone(sched.get_job(f'start_{rid}'))
        self.assertIsNotNone(sched.get_job(f'stop_{rid}'))

        # Form route redirects to the index on success (302); the JSON twin returns 200.
        resp = self.t.client.post(f'/recordings/{rid}/delete')
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))

        self.assertIsNone(db.session.get(Recording, rid))
        self.assertIsNone(sched.get_job(f'start_{rid}'), 'start job leaked after delete')
        self.assertIsNone(sched.get_job(f'stop_{rid}'), 'stop job leaked after delete')
        # delete-orphan cascade: no dangling child rows
        self.assertEqual(RecordingSegment.query.filter_by(recording_id=rid).count(), 0)
        self.assertEqual(RecordingEvent.query.filter_by(recording_id=rid).count(), 0)


class CancelScheduledEmitsEventTests(unittest.TestCase):
    """Cancelling a SCHEDULED recording must leave a RECORDING_ABORTED event on the row,
    same as the IN_PROGRESS path already does - guards dev/docs/BUGS.md 2026-07-25 @ ~08:00 PM ET."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_cancel_scheduled_leaves_aborted_event(self):
        future = datetime.utcnow() + timedelta(days=3650)
        rec = seed.make_recording(status='SCHEDULED', channel_id=self.ch.id,
                                  start_time=future, stop_time=future + timedelta(hours=1))
        db.session.commit()
        rid = rec.id
        schedule_recording(self.t.app, rid, rec.start_time, rec.stop_time)

        resp = self.t.client.post(f'/recordings/{rid}/cancel')
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))

        self.assertEqual(db.session.get(Recording, rid).status, 'ABORTED')
        events = RecordingEvent.query.filter_by(recording_id=rid).all()
        self.assertTrue(
            any(e.event_type == 'RECORDING_ABORTED' for e in events),
            f'no RECORDING_ABORTED event on cancelled SCHEDULED recording; got {[e.event_type for e in events]}')


class CancelJsonRejectsTerminalStatusTests(unittest.TestCase):
    """cancel_recording_json must reject an already-finished recording instead of falling
    through to abort_recording and rewriting its terminal state - guards
    dev/docs/BUGS.md 2026-08-15 @ ~12:00 AM ET."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_completed_recording_rejected_and_untouched(self):
        completed_at = datetime.utcnow() - timedelta(days=30)
        rec = seed.make_recording(status='COMPLETED', channel_id=self.ch.id,
                                  completed_at=completed_at)
        db.session.commit()
        rid = rec.id
        stop_time = rec.stop_time

        resp = self.t.client.post(f'/recordings/{rid}/cancel-json')
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn('error', resp.get_json())

        db.session.expire_all()
        fresh = db.session.get(Recording, rid)
        self.assertEqual(fresh.status, 'COMPLETED')
        self.assertEqual(fresh.completed_at, completed_at)
        self.assertEqual(fresh.stop_time, stop_time)
        self.assertEqual(
            RecordingEvent.query.filter_by(recording_id=rid, event_type='RECORDING_ABORTED').count(),
            0, 'a RECORDING_ABORTED event was logged against an already-completed recording')

    def test_failed_and_aborted_also_rejected(self):
        for status in ('FAILED', 'ABORTED'):
            rec = seed.make_recording(status=status, channel_id=self.ch.id)
            db.session.commit()
            resp = self.t.client.post(f'/recordings/{rec.id}/cancel-json')
            self.assertEqual(resp.status_code, 400, f'{status}: {resp.get_data(as_text=True)}')


class AbortAndDeleteFilesTests(unittest.TestCase):
    """The shared _abort_and_delete_files helper (both cancel paths funnel through it)
    must remove every segment file AND the live thumbnail, and mark the row ABORTED."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_abort_deletes_segments_thumbnail_and_marks_aborted(self):
        from app.config import load_config

        # Every file this test writes lands under the test app's own temp dir, never the
        # operator's real /dvr. Segment paths come off the RecordingSegment rows, so they
        # relocate for free; the thumbnail path does not exist on any row - production
        # resolves it inside recorder.recording_disk_paths from a runtime load_config(),
        # which make_test_app's overrides cannot reach - so that one lookup is patched.
        # Before this, the test wrote into whatever dvr_output_dir the machine's real
        # config.yaml named and errored outright when that directory did not exist, which
        # is how the clean-room run found it (dev/changelog/520).
        dvr_dir = os.path.join(self.t._tmpdir, 'incomplete')
        images_dir = os.path.join(self.t._tmpdir, 'images')
        thumb_dir = os.path.join(images_dir, 'thumbnails')
        os.makedirs(dvr_dir, exist_ok=True)
        os.makedirs(thumb_dir, exist_ok=True)
        cfg = load_config()
        cfg['recording']['dvr_output_dir'] = dvr_dir
        cfg['recording']['images_dir'] = images_dir

        rec = seed.make_recording(status='PAUSED', channel_id=self.ch.id)
        db.session.flush()
        rid = rec.id

        seg_paths = []
        for i in range(2):
            p = os.path.join(dvr_dir, f'seg_{rid}_{i}.ts')
            with open(p, 'wb') as f:
                f.write(b'\x00' * 32)
            seg_paths.append(p)
            db.session.add(RecordingSegment(
                recording_id=rid, segment_number=i, file_path=p,
                started_at=datetime.utcnow(), exit_reason='STALL_KILLED',
                bytes_recorded=32))
        thumb_path = os.path.join(thumb_dir, f'{rid}.jpg')
        with open(thumb_path, 'wb') as f:
            f.write(b'\xff\xd8\xff')
        db.session.commit()

        from app.routes.recordings import _abort_and_delete_files
        with mock.patch('app.recorder.load_config', return_value=cfg):
            _abort_and_delete_files(rid)

        for p in seg_paths:
            self.assertFalse(os.path.exists(p), f'segment file left on disk: {p}')
        self.assertFalse(os.path.exists(thumb_path), 'live thumbnail left on disk')
        self.assertEqual(db.session.get(Recording, rid).status, 'ABORTED')


class LaunchFailureGiveUpReleasesEverythingTests(_LaunchRetryTestCase):
    """The FAILED path reached when ffmpeg's own spawn keeps failing.

    Guards dev/docs/BUGS.md 2026-08-18 @ 07:34 AM "A launch-failure give-up leaks the
    account's connection slot" - the same omission dev/changelog/464 fixed for the
    watchdog's give-up, in the one terminal path that fix never reached. The rig this
    subclasses uses a channel-less recording precisely to keep slot acquisition out of
    its way, which is why nothing here was covered.

    Reuses that rig's temp dvr dir and fast-retry profile; adds the account, channel and
    real slot acquisition a channel-backed recording's start path performs.
    """

    max_failures = 1

    def setUp(self):
        super().setUp()
        acct = seed.make_account(name='One Slot', max_connections=1)
        self.channel = seed.make_channel(acct, name='Give-up Channel')
        db.session.commit()
        self.account_id = acct.id
        connlim._holders.clear()
        self.addCleanup(connlim._holders.clear)

    def _channel_recording(self, status='SCHEDULED'):
        rec = seed.make_recording(status=status, name='Launch Failure',
                                  channel_id=self.channel.id, profile_id=self.profile.id)
        db.session.commit()
        return rec

    def _failing_popen(self, recording_id, captured):
        """A Popen that always raises, and records what the recording was holding at the
        moment of the spawn - the give-up pops _active, so this is the only point at which
        a test can see the live state it is about to assert was released."""
        def _boom(*args, **kwargs):
            captured.setdefault('state', recorder.get_state(recording_id))
            captured.setdefault('holders', self._holders())
            raise OSError('ffmpeg not found')
        return _boom

    def _holders(self):
        return list(connlim._holders.get(self.account_id, []))

    def test_the_give_up_releases_the_accounts_connection_slot(self):
        rec = self._channel_recording()
        rid = rec.id
        captured = {}
        with mock.patch.object(recorder.subprocess, 'Popen',
                               side_effect=self._failing_popen(rid, captured)):
            recorder.start_recording(self.t.app, rid)

        self.assertEqual(captured['holders'], [('recording', rid)],
                         'precondition: the start path never acquired a slot to leak')
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, 'FAILED',
                         'precondition: this must be the terminal give-up, not a retry')
        self.assertEqual(
            self._holders(), [],
            "the give-up left the dead recording holding the account's connection slot")
        # The concrete consequence, the same one recording 73 -> 75 demonstrated for the
        # watchdog path: a later recording on this account must be able to connect.
        self.assertTrue(connlim.try_acquire(self.account_id, 'recording', 99999),
                        'a later recording still could not get the slot the dead one leaked')

    def test_the_give_up_leaves_no_live_state_and_cancels_a_pending_retry(self):
        """stop_event is what a launch-retry thread waits on (dev/changelog/644), so a
        give-up that only pops _active leaves one sleeping out its delay against a
        recording that is already terminal."""
        rec = self._channel_recording()
        rid = rec.id
        captured = {}
        with mock.patch.object(recorder.subprocess, 'Popen',
                               side_effect=self._failing_popen(rid, captured)):
            recorder.start_recording(self.t.app, rid)

        self.assertIsNone(recorder.get_state(rid), '_active still holds the dead recording')
        self.assertIsNotNone(captured['state'], 'precondition: no live state was ever tracked')
        self.assertTrue(captured['state'].stop_event.is_set(),
                        'the give-up never signalled the state it abandoned')

    def test_a_mid_recording_give_up_keeps_what_was_already_captured(self):
        """The deliberate non-release, and the boundary of this rule: abort deletes
        segments, a give-up must not. A FAILED recording is still concatenated and
        salvaged (Product Principle 2), so deleting its segments here would destroy the
        only copy of the capture."""
        rec = self._channel_recording(status='IN_PROGRESS')
        rid = rec.id
        seg_path = os.path.join(self.dvr, f'kept_{rid}_seg_001.ts')
        with open(seg_path, 'wb') as fh:
            fh.write(b'\x47' * 4096)
        db.session.add(RecordingSegment(
            recording_id=rid, segment_number=1, file_path=seg_path,
            started_at=datetime.utcnow(), ended_at=datetime.utcnow(),
            exit_reason='STALL_KILLED', bytes_recorded=4096))
        db.session.commit()

        state = recorder.RecordingState(current_segment_num=1)
        with recorder._lock:
            recorder._active[rid] = state
        self.assertTrue(connlim.try_acquire(self.account_id, 'recording', rid),
                        'setup could not take the slot the give-up is meant to release')

        with mock.patch.object(recorder.subprocess, 'Popen',
                               side_effect=OSError('ffmpeg not found')):
            recorder._launch_segment(self.t.app, rid, seg_num=2)

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, 'FAILED')
        self.assertTrue(os.path.exists(seg_path),
                        'the give-up deleted the segments it was supposed to salvage')
        self.assertEqual(
            RecordingSegment.query.filter_by(recording_id=rid).count(), 1,
            'the give-up deleted the segment row for a recording that still needs it')
        self.assertEqual(self._holders(), [], 'the connection slot was left held')
        self.assertIsNone(recorder.get_state(rid), '_active still holds the dead recording')
        self.assertTrue(state.stop_event.is_set(),
                        'the give-up never signalled the state it abandoned')


class WatchdogGiveUpReleasesEverythingTests(_ConnectionReleaseHarness):
    """The other FAILED path: the watchdog abandoning a recording it cannot restart.

    The connection slot half is guarded next door by
    tests/test_watchdog_connection_release.py and is deliberately not repeated. This
    enumerates the rest of what the create path acquired - the ffmpeg child, the watchdog
    thread itself, the _active entry, and the segment's stderr spool - plus the segments
    a give-up must leave alone.
    """

    def setUp(self):
        super().setUp()
        self.relaunched = []
        self.addCleanup(self._kill_relaunched)

    def _kill_relaunched(self):
        for proc in self.relaunched:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def _stub_launch_live_but_empty(self):
        """A relaunch that spawns a real child and never writes a byte.

        This is the state _mark_recording_failed's kill exists for, in its own words:
        the last restart attempt's ffmpeg is still running, it just has not produced data
        yet. _RestartHarness's produces_data=False stub hands over an already-dead process
        instead, which lets the stall path's kill satisfy an assertion meant for the
        give-up's - the test passed against a give-up that killed nothing until this rig
        replaced it.
        """
        def _stub(app, recording_id, seg_num):
            path = os.path.join(self.t._tmpdir, f'rec_{self.rid}_seg_{seg_num:03d}.ts')
            open(path, 'wb').close()
            db.session.add(RecordingSegment(recording_id=recording_id,
                                            segment_number=seg_num, file_path=path,
                                            started_at=datetime.utcnow()))
            db.session.commit()
            self.state.process = _sleeper()
            self.relaunched.append(self.state.process)
            self.state.current_segment_num = seg_num
        return _stub

    def _run_until_give_up(self, event_type, timeout=60):
        """Deliberately never sets stop_event, unlike _RestartHarness._run_until_event:
        the give-up has to wind its own thread down, and setting the flag here would make
        the thread-exit assertion below pass for the wrong reason. tearDown sets it
        regardless, so a give-up that hangs cannot hang the suite."""
        with mock.patch.object(cfgmod, 'load_config', return_value=self._cfg(1, 0)), \
             mock.patch.object(recorder, '_launch_segment',
                               self._stub_launch_live_but_empty()), \
             mock.patch.object(recorder, 'failover_group_member', return_value=False):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            deadline = time.monotonic() + timeout
            seen = False
            while time.monotonic() < deadline:
                db.session.expire_all()
                seen = bool(RecordingEvent.query.filter_by(
                    recording_id=self.rid, event_type=event_type).first())
                if seen:
                    break
                time.sleep(0.2)
            self.assertTrue(seen, f'no {event_type} event within {timeout}s')
            self.wd.join(timeout=20)
        db.session.expire_all()

    def _spools_on_disk(self):
        log_dir = self.t.app.config.get('CAPTURE_LOG_DIR')
        return glob.glob(os.path.join(log_dir, f'.cap-stderr-{self.rid}-*.log'))

    def test_a_give_up_releases_the_process_thread_state_and_spool(self):
        # 2, not 1: at 1 the very first stall gives up before any restart, and the stall
        # handling has already killed the process by then - so the give-up's own kill is
        # a no-op and this test would pass against a give-up that released nothing. The
        # branch worth guarding is the one after a restart that produced no data, where
        # the replacement ffmpeg is still running.
        self._extra_watchdog_cfg = {'max_consecutive_failures': 2}
        self.state.process = _sleeper()
        self.relaunched.append(self.state.process)
        path, fh = recorder._open_segment_stderr_spool(self.t.app, self.rid, 0)
        self.state.stderr_path, self.state.stderr_fh = path, fh
        self.assertEqual(len(self._spools_on_disk()), 1,
                         'precondition: setup opened no spool for the give-up to release')

        self._run_until_give_up(RECORDING_FAILED)

        self.assertEqual(db.session.get(Recording, self.rid).status, 'FAILED')
        self.assertTrue(self.relaunched, 'precondition: no restart was ever attempted')
        self.assertIsNotNone(
            self.relaunched[-1].poll(),
            'the give-up abandoned a live ffmpeg child, which keeps holding the '
            "provider's connection untracked")
        self.assertFalse(self.wd.is_alive(),
                         'the watchdog kept polling a recording it had already abandoned')
        self.assertIsNone(recorder.get_state(self.rid),
                          '_active still holds the abandoned recording')
        self.assertEqual(self._spools_on_disk(), [],
                         'the give-up left a capture stderr spool on disk')
        self.assertTrue(os.path.exists(self.seg_path),
                        'the give-up deleted the segment it was supposed to salvage')


def _sigterm_ignoring_child():
    """A capture that refuses SIGTERM - what forces kill_all_active's escalation to
    SIGKILL. ffmpeg can genuinely sit in a flush and ignore a terminate, and an ignored
    one at shutdown is precisely the orphan this function exists to prevent."""
    return subprocess.Popen(
        [sys.executable, '-c',
         'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
         'time.sleep(120)'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class ShutdownReleasesEverythingTests(unittest.TestCase):
    """kill_all_active - the shutdown/restart terminal path, run from run.py's signal
    handler.

    It deliberately does no DB work and pops nothing (a signal handler cannot), so what
    it owes is narrow and entirely about what outlives the process: no ffmpeg child may
    survive to keep writing a segment file and holding the provider's connection, every
    watchdog must be told this death was deliberate, and everything already captured must
    survive for the next process to resume. The one existing exercise of this function
    (test_watchdog_process_exit.DeliberateKillIsNotADeadFeedTests) asserts event
    semantics for a single recording; these assert the cleanup, across more than one.
    """

    def setUp(self):
        self.t = make_test_app()
        self.tracked = []
        self.addCleanup(self._kill_leftovers)

    def tearDown(self):
        self.t.cleanup()

    def _kill_leftovers(self):
        for _rid, _state, proc in self.tracked:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def _assert_dead(self, proc, msg, timeout=10):
        """Bounded wait, not a bare poll(): kill_all_active does not wait() after the
        SIGKILL it escalates to, so the child can still be unreaped the instant it
        returns and a bare poll() decides this test on scheduling luck. A capture that
        was never signalled sleeps for two minutes, so an exhausted deadline is a real
        failure rather than a slow machine."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.05)
        self.assertIsNotNone(proc.poll(), msg)

    def _live_recording(self, name, proc):
        """One IN_PROGRESS recording with a segment on disk and a tracked live capture -
        the state start_recording would have left behind."""
        now = datetime.utcnow()
        rec = seed.make_recording(status='IN_PROGRESS', name=name, started_at=now,
                                  start_time=now, stop_time=now + timedelta(hours=1))
        db.session.flush()
        seg_path = os.path.join(self.t._tmpdir, f'shutdown_{rec.id}_seg_001.ts')
        with open(seg_path, 'wb') as fh:
            fh.write(b'\x47' * 2048)
        db.session.add(RecordingSegment(recording_id=rec.id, segment_number=1,
                                        file_path=seg_path, started_at=now))
        db.session.commit()
        state = recorder.RecordingState(current_segment_num=1)
        state.process = proc
        with recorder._lock:
            recorder._active[rec.id] = state
        self.addCleanup(lambda rid=rec.id: recorder._active.pop(rid, None))
        self.tracked.append((rec.id, state, proc))
        return rec.id, state, seg_path

    def test_every_tracked_capture_is_killed_not_just_the_first(self):
        """The three loops in kill_all_active each run over the whole set; collapsing any
        of them to the first entry would leave the other recordings' ffmpeg children alive
        and writing after the process that owned them is gone."""
        _rid_a, _state_a, _ = self._live_recording('shutdown-a', _sleeper())
        _rid_b, _state_b, _ = self._live_recording('shutdown-b', _sleeper())

        recorder.kill_all_active()

        for rid, _state, proc in self.tracked:
            self._assert_dead(proc, f'recording {rid} kept its ffmpeg child after shutdown')

    def test_every_watchdog_is_told_the_death_was_deliberate(self):
        """stop_event is the app-wide "we killed it" flag: a watchdog that does not get it
        reads the shutdown as a stall and spawns a replacement ffmpeg on the way out. It is
        also what a pending launch-retry thread waits on, so both must wind down for every
        recording, not just the one that happened to be first."""
        _rid_a, state_a, _ = self._live_recording('shutdown-a', _sleeper())
        _rid_b, state_b, _ = self._live_recording('shutdown-b', _sleeper())
        exited = []
        threads = []
        for label, state in (('a', state_a), ('b', state_b)):
            th = threading.Thread(target=lambda l=label, s=state: (
                s.stop_event.wait(timeout=20), exited.append(l)), daemon=True)
            th.start()
            threads.append(th)

        recorder.kill_all_active()

        for th in threads:
            th.join(timeout=15)
        self.assertTrue(state_a.stop_event.is_set())
        self.assertTrue(state_b.stop_event.is_set())
        self.assertEqual(sorted(exited), ['a', 'b'],
                         'a thread waiting on stop_event was never released by the shutdown')

    def test_a_capture_that_ignores_sigterm_is_still_killed(self):
        """The escalation branch, otherwise unexercised. Costs the 5s wait
        kill_all_active gives a child before SIGKILL - a real wall-clock cost accepted
        deliberately, because the alternative is that the one path which stops a
        SIGTERM-ignoring ffmpeg from outliving the app has no test at all."""
        rid, _state, _ = self._live_recording('stubborn', _sigterm_ignoring_child())

        recorder.kill_all_active()

        self._assert_dead(
            self.tracked[-1][2],
            f'recording {rid}: a capture that ignored SIGTERM survived the shutdown')

    def test_what_was_captured_survives_for_the_next_process_to_resume(self):
        """The deliberate non-release. Segment files and their rows are how a restarted
        service picks the recording back up (scheduler.resume_in_progress_recordings), so
        a shutdown that tidied them away would destroy the capture it is protecting."""
        rid, _state, seg_path = self._live_recording('shutdown-a', _sleeper())

        recorder.kill_all_active()

        db.session.expire_all()
        self.assertTrue(os.path.exists(seg_path), 'the shutdown deleted a segment file')
        self.assertEqual(RecordingSegment.query.filter_by(recording_id=rid).count(), 1,
                         'the shutdown deleted a segment row')
        self.assertEqual(db.session.get(Recording, rid).status, 'IN_PROGRESS',
                         'the shutdown rewrote a recording status from a signal handler')


class DeleteUnlinksWhatNamesTheRecordingTests(unittest.TestCase):
    """Deleting a recording must take its alerts with it and leave nothing holding its id.

    The teardown rule applied to the rows that reference a recording but are not cascaded
    away with it. `recordings` was a plain INTEGER PRIMARY KEY when these were written, so
    SQLite re-issued a deleted row's number to the next recording created: an alert left
    holding it did not dangle, it silently re-attached to an unrelated recording and
    deep-linked to it (dev/docs/BUGS.md 2026-09-11 @ 08:55:00 PM ET, dev/changelog/929).
    The table now carries AUTOINCREMENT (dev/changelog/937), which retires the number
    instead - but the unlinking is what these assert, and it is what still covers an id
    re-issued before that shipped and every row that would otherwise name a recording that
    no longer exists.
    """

    #: A dismissal that predates the delete, so a test can prove the delete did not move it.
    OLD_DISMISSAL = datetime(2026, 1, 1, 0, 0, 0)

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _recording_with_alerts(self, status='COMPLETED', **kw):
        """A recording plus the two alert shapes a delete has to handle: one still open,
        one dismissed long ago. Returns (recording_id, open_alert_id, dismissed_alert_id)."""
        rec = seed.make_recording(status=status, channel_id=self.ch.id, **kw)
        db.session.flush()
        open_alert = Alert(alert_type='LOG_ERROR', severity='ERROR',
                           title=f'Recording "{rec.name}" (#{rec.id}) move failed',
                           source='app.postprocessor', recording_id=rec.id)
        old_alert = Alert(alert_type='RECORDING_CHANNEL_FAILING', severity='WARN',
                          title='Scheduled recording channel failing',
                          source=f'recfail:rec:{rec.id}:ch:{self.ch.id}',
                          recording_id=rec.id, dismissed_at=self.OLD_DISMISSAL)
        db.session.add_all([open_alert, old_alert])
        db.session.commit()
        return rec.id, open_alert.id, old_alert.id

    def test_delete_dismisses_and_unlinks_every_alert_naming_the_recording(self):
        rid, open_id, old_id = self._recording_with_alerts()

        resp = self.t.client.post(f'/recordings/{rid}/delete')
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))

        db.session.expire_all()
        self.assertIsNone(db.session.get(Recording, rid))
        self.assertEqual(Alert.query.filter_by(recording_id=rid).count(), 0,
                         'an alert still carries the deleted recording id')
        self.assertIsNotNone(db.session.get(Alert, open_id).dismissed_at,
                             'the open alert about a deleted recording was left open')
        self.assertEqual(db.session.get(Alert, old_id).dismissed_at, self.OLD_DISMISSAL,
                         'the delete rewrote a dismissal that had already happened')

    def test_the_next_recording_gets_a_fresh_id_and_no_inherited_alerts(self):
        """Both halves of the fix, which are belt and braces rather than alternatives.

        `recordings` is AUTOINCREMENT (dev/changelog/937), so the deleted recording's number
        is retired instead of being handed to the next one - this used to assert the
        opposite, because until that shipped the reuse was real and the test's job was to
        pin the unlinking that made it survivable. The unlinking (dev/changelog/929) stays
        and is still asserted here: it is what protects an id that was already re-issued
        before the rebuild ran, and any future column that stores one.
        """
        rid, _open_id, _old_id = self._recording_with_alerts()

        resp = self.t.client.post(f'/recordings/{rid}/delete')
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))

        successor = seed.make_recording(status='SCHEDULED', channel_id=self.ch.id,
                                        name='the next recording')
        db.session.commit()
        self.assertGreater(successor.id, rid,
                           'SQLite re-issued a deleted recording id - recordings lost its '
                           'AUTOINCREMENT primary key')
        self.assertEqual(Alert.query.filter_by(recording_id=successor.id).count(), 0,
                         'a new recording inherited the deleted one\'s alerts with its id')

    def test_cancelling_a_scheduled_recording_unlinks_its_alerts(self):
        """Cancel deletes the row outright (dev/changelog/814), so it is a delete path."""
        future = datetime.utcnow() + timedelta(days=3650)
        rid, open_id, _old_id = self._recording_with_alerts(
            status='SCHEDULED', start_time=future, stop_time=future + timedelta(hours=1))

        resp = self.t.client.post(f'/recordings/{rid}/cancel-json')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        db.session.expire_all()
        self.assertIsNone(db.session.get(Recording, rid))
        self.assertEqual(Alert.query.filter_by(recording_id=rid).count(), 0)
        self.assertIsNotNone(db.session.get(Alert, open_id).dismissed_at)

    def test_the_retention_sweep_unlinks_the_alerts_of_what_it_deletes(self):
        """The delete path with nobody watching: it runs on a schedule, so the rows it
        strands are the ones that sit for days before anyone sees them."""
        long_ago = datetime.utcnow() - timedelta(days=90)
        rid, open_id, _old_id = self._recording_with_alerts(
            status='COMPLETED', completed_at=long_ago)

        # A runtime load_config() reads the real config.yaml, so the retention window has
        # to be patched rather than passed as a make_test_app override (CLAUDE.md §Testing).
        with mock.patch.object(cfgmod, 'load_config', return_value={
                'recording': {'retention_days': 1, 'retention_delete_file': False}}):
            sched_mod._recording_retention_sweep()

        db.session.expire_all()
        self.assertIsNone(db.session.get(Recording, rid),
                          'the sweep did not delete a recording past its window')
        self.assertEqual(Alert.query.filter_by(recording_id=rid).count(), 0)
        self.assertIsNotNone(db.session.get(Alert, open_id).dismissed_at)

    def test_replacing_a_scheduled_recording_unlinks_the_one_it_deletes(self):
        """Find Another Airing deletes the recording it replaces, in a closure of its own -
        the fourth delete path, and the one a grep for the delete route would miss."""
        future = datetime.utcnow() + timedelta(days=3650)
        rid, open_id, _old_id = self._recording_with_alerts(
            status='SCHEDULED', start_time=future, stop_time=future + timedelta(hours=1))

        # The form parses local time and converts to UTC, so these are display-local.
        start = datetime.now() + timedelta(days=2)
        stop = start + timedelta(hours=1)
        resp = self.t.client.post('/recordings/new-json', data={
            'name': 'the replacement',
            'url': 'http://example.test/live/9',
            'start_time': start.strftime('%Y-%m-%dT%H:%M'),
            'stop_time': stop.strftime('%Y-%m-%dT%H:%M'),
            'replace_recording_id': str(rid),
        })
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        db.session.expire_all()
        self.assertIsNone(db.session.get(Recording, rid))
        self.assertEqual(Alert.query.filter_by(recording_id=rid).count(), 0)
        self.assertIsNotNone(db.session.get(Alert, open_id).dismissed_at)

    def test_a_pre_check_test_stops_pointing_at_the_deleted_recording(self):
        """channel_tests.pre_check_recording_id is not cascaded either, and a group page
        scopes health checks by recording id - so a reused id would pull an old
        recording's tests onto a new one."""
        rid, _open_id, _old_id = self._recording_with_alerts()
        test_row = seed.make_channel_test(self.ch, pre_check_recording_id=rid)
        db.session.commit()
        test_id = test_row.id

        resp = self.t.client.post(f'/recordings/{rid}/delete')
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))

        db.session.expire_all()
        self.assertIsNotNone(db.session.get(ChannelTest, test_id),
                             'the measurement itself must survive; only the link goes')
        self.assertIsNone(db.session.get(ChannelTest, test_id).pre_check_recording_id)


if __name__ == '__main__':
    unittest.main(verbosity=2)
