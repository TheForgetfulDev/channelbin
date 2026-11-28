"""The watchdog test rig: one recording, one open segment, one real WatchdogThread.

Shared by every watchdog test module (process exit, downtime accounting, dead-stream retry,
failure cause, restart-no-data, segment teardown, connection release, stall-rate failover,
launch-failure ownership, early-fail-off, capture teardown races). It lived in
tests/test_watchdog_process_exit.py and tests/test_downtime_accounting.py and was imported
from there by the rest, which coupled nine files to those two modules' incidental contents;
tests/support/ is where the suite's shared rigs belong (dev/changelog/1246).

Design and reasoning for the rig itself: dev/changelog/431 (WatchdogHarness) and
dev/changelog/432 (RestartHarness, the half that lets a restart really produce - or fail
to produce - a next segment so the delay and the reconnect wait actually elapse).

No network and no provider host: every child here is `sys.executable -c ...`, a local argv
with no URL in it, which tests/support/netguard.py permits. Segment files are written under
make_test_app's temp dir, never /dvr.
"""
import json
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import app.config as cfgmod
import app.recorder as recorder
from app import db
from app.database import STALL_DETECTED, Recording, RecordingEvent, RecordingSegment
from app.recorder import RecordingState
from app.watchdog import WatchdogThread
from tests.support import seed
from tests.support.app import make_test_app


# A stand-in capture: writes ffmpeg-shaped progress lines into the spool it inherits as
# stderr, advancing the content position `rate` times faster than the wall clock, and stays
# alive so the watchdog sees a running process rather than one that exited.
FAKE_PROGRESS_CAPTURE = (
    'import sys, time\n'
    'rate = float(sys.argv[1])\n'
    'for _ in range(int(sys.argv[2]) if len(sys.argv) > 2 else 0):\n'
    '    sys.stderr.write("[http @ 0x1] Will reconnect at 22474200 in 0 second(s), '
    'error=Connection timed out.\\n")\n'
    'replay = len(sys.argv) > 3\n'
    'step = 0.1\n'
    'content = 0.0\n'
    'tick = 0\n'
    'while True:\n'
    # A feed fast only by its replays runs at real time between reconnects; each reconnect
    # then moves the position by the replay in one jump, after the reconnect notice and
    # before the next position line - where ffmpeg puts it. Spreading the replay over the
    # ticks instead, with the notice every fifth, read as 2.0x "own" speed whenever a loaded
    # box stretched the tick and a 1 s window held 8 ticks and one notice (dev/docs/BUGS.md
    # 2026-11-09). `rate` is still what the detector reads over any whole number of cycles.
    '    content += step * (1.0 if replay else rate)\n'
    '    tick += 1\n'
    '    h, rem = divmod(content, 3600)\n'
    '    m, s = divmod(rem, 60)\n'
    '    sys.stderr.write("frame=1 time=%02d:%02d:%05.2f speed=%.2fx    \\r" % (h, m, s, rate))\n'
    '    if replay and tick % 5 == 0:\n'
    '        jump = 5 * step * (rate - 1)\n'
    '        sys.stderr.write("[http @ 0x1] Will reconnect at 1 in 0 second(s), '
    'error=Input/output error.\\n[vist#0:0/h264 @ 0x2] timestamp discontinuity (stream id=256): '
    '%d, new offset= 1\\n" % -int(jump * 1000000))\n'
    '        content += jump\n'
    '    sys.stderr.flush()\n'
    '    time.sleep(step)\n'
)


def sleeper(seconds=120):
    """A live local child that outlives the test unless something kills it."""
    return subprocess.Popen([sys.executable, '-c', f'import time; time.sleep({seconds})'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class WatchdogHarness(unittest.TestCase):
    """One recording, one open segment on a static file, one WatchdogThread under our own
    config. _launch_segment is stubbed throughout: a restart is not what any of these
    assert, and letting the real one run would spawn ffmpeg at a provider URL."""

    def setUp(self):
        self.t = make_test_app()
        self.wd = None
        self.launched = []
        self.state = RecordingState(current_segment_num=0)

        now = datetime.utcnow()
        rec = seed.make_recording(status='IN_PROGRESS', name='wdexit', started_at=now,
                                  start_time=now, stop_time=now + timedelta(hours=1))
        self.rid = rec.id
        self.seg_path = os.path.join(self.t._tmpdir, f'rec_{self.rid}_seg_000.ts')
        with open(self.seg_path, 'wb') as fh:
            fh.write(b'\x47' * 4096)
        db.session.add(RecordingSegment(recording_id=self.rid, segment_number=0,
                                        file_path=self.seg_path, started_at=now))
        db.session.commit()

        with recorder._lock:
            recorder._active[self.rid] = self.state

    def tearDown(self):
        self.state.stop_event.set()
        if self.wd is not None:
            self.wd.join(timeout=15)
        proc = self.state.process
        if isinstance(proc, subprocess.Popen) and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        with recorder._lock:
            recorder._active.pop(self.rid, None)
        self.t.cleanup()

    def _window_too_short_to_retry(self):
        """End the rig's window 40 s from now, under the minute a last retry leaves to capture
        (watchdog._LAST_RETRY_LEFT). With the window open a dead stream waits and tries again
        (dev/changelog/1793), so a test of the end of a capture starts here."""
        rec = db.session.get(Recording, self.rid)
        rec.stop_time = datetime.utcnow() + timedelta(seconds=40)
        db.session.commit()

    def _nothing_captured(self):
        """Empty the rig's segment file. A give-up on a capture holding data ends it and joins
        what it holds (dev/changelog/1404), so a test of the FAILED path starts here."""
        open(self.seg_path, 'wb').close()

    def _exited_capture(self, code=1, stderr_text='Connection reset by peer\n'):
        """An ffmpeg stand-in that has ALREADY exited, spooling stderr the way the real
        capture does, so the exit code and tail this path records are real values."""
        path, fh = recorder._open_segment_stderr_spool(self.t.app, self.rid, 0)
        self.state.stderr_path, self.state.stderr_fh = path, fh
        proc = subprocess.Popen(
            [sys.executable, '-c',
             f'import sys; sys.stderr.write({stderr_text!r}); sys.stderr.flush(); '
             f'sys.exit({code})'],
            stdout=subprocess.DEVNULL, stderr=(fh or subprocess.DEVNULL))
        proc.wait(timeout=30)
        self.state.process = proc
        return proc

    def _cfg(self, stall_timeout, restart_delay=0):
        return cfgmod._deep_merge(cfgmod.load_config(), {'watchdog': {
            'poll_interval_seconds': 1,
            'stall_timeout_seconds': stall_timeout,
            'restart_delay_seconds': restart_delay,
            'max_consecutive_failures': 99,
            'early_fail_abort_count': 99,
        }})

    def _stub_launch(self, new_process=None):
        """Stand-in for _launch_segment. Sets stop_event so the watchdog winds down after
        one restart instead of looping; the real one is checked by its own tests.

        Returns LAUNCH_SPAWNED because that is what the real function returns when a child
        is running, and the watchdog's restart branch reads it: anything else means the
        spawn failed and the branch skips its own accounting entirely (dev/changelog/984).
        A stub returning None would silently turn every restart in this file into a failed
        launch."""
        def _stub(app, recording_id, seg_num):
            self.launched.append(seg_num)
            if new_process is not None:
                self.state.process = new_process
            self.state.stop_event.set()
            return recorder.LAUNCH_SPAWNED
        return _stub

    def _run_until_segment_closed(self, stall_timeout, new_process=None, timeout=45):
        """Start the real thread; return (segment row, seconds until it was closed)."""
        started = time.monotonic()
        elapsed = None
        with mock.patch.object(cfgmod, 'load_config',
                               return_value=self._cfg(stall_timeout)), \
             mock.patch.object(recorder, '_launch_segment',
                               self._stub_launch(new_process)):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                db.session.expire_all()
                seg = RecordingSegment.query.filter_by(
                    recording_id=self.rid, segment_number=0).first()
                if seg.ended_at is not None:
                    elapsed = time.monotonic() - started
                    break
                time.sleep(0.2)
            self.state.stop_event.set()
            self.wd.join(timeout=15)
        db.session.expire_all()
        seg = RecordingSegment.query.filter_by(
            recording_id=self.rid, segment_number=0).first()
        return seg, elapsed

    def _stall_event(self):
        db.session.expire_all()
        return (RecordingEvent.query
                .filter_by(recording_id=self.rid, event_type=STALL_DETECTED)
                .order_by(RecordingEvent.id.desc()).first())


class RestartHarness(WatchdogHarness):
    """Adds the half the process-exit rig deliberately skipped: a _launch_segment stub
    that really produces (or fails to produce) a next segment, so the restart delay and
    the reconnect wait actually elapse and can be measured."""

    def _stub_launch_next(self, *, produces_data):
        """Stand-in for _launch_segment that creates the next segment row, optionally
        writes a byte to it, and leaves stop_event alone so the restart resolves normally.
        A non-producing restart is handed an already-dead process, which is what makes
        wait_for_file_data give up at once instead of burning the stall timeout.

        Returns LAUNCH_SPAWNED, the real function's answer when a child is running - the
        watchdog's restart branch skips all of its own accounting for any other value
        (dev/changelog/984), which is the whole scenario this harness exists to drive."""
        def _stub(app, recording_id, seg_num):
            self.launched.append(seg_num)
            path = os.path.join(self.t._tmpdir, f'rec_{self.rid}_seg_{seg_num:03d}.ts')
            if produces_data:
                with open(path, 'wb') as fh:
                    fh.write(b'\x47' * 1024)
                self.state.process = sleeper()
            else:
                open(path, 'wb').close()
            db.session.add(RecordingSegment(recording_id=recording_id,
                                            segment_number=seg_num, file_path=path,
                                            started_at=datetime.utcnow()))
            db.session.commit()
            self.state.current_segment_num = seg_num
            return recorder.LAUNCH_SPAWNED
        return _stub

    def _run_until_event(self, event_type, *, stall_timeout, restart_delay,
                         produces_data, timeout=60):
        """Start the real thread and stop it once the restart has resolved."""
        with mock.patch.object(cfgmod, 'load_config',
                               return_value=self._cfg(stall_timeout, restart_delay)), \
             mock.patch.object(recorder, '_launch_segment',
                               self._stub_launch_next(produces_data=produces_data)):
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
            self.state.stop_event.set()
            self.wd.join(timeout=15)
        self.assertTrue(seen, f'no {event_type} event within {timeout}s')
        db.session.expire_all()
        return db.session.get(Recording, self.rid)

    def _stall_seconds(self):
        """The FIRST stall's detection window, as the event recorded it - the same number
        the counter is supposed to have banked for it. Deliberately not the latest: a
        restart that produces nothing leaves a dead process on an empty segment, so the
        watchdog immediately declares a second, near-zero PROCESS_EXITED stall, and
        anchoring on that one would compare the total against a window of 0.0 and pass
        against anything."""
        evt = (RecordingEvent.query
               .filter_by(recording_id=self.rid, event_type=STALL_DETECTED)
               .order_by(RecordingEvent.id.asc()).first())
        return json.loads(evt.extra_data)['stall_duration']
