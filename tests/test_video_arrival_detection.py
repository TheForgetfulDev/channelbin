"""Tier 2 - a capture whose video stops while the file keeps growing is said out loud, and
restarted for the picture.

Evidence, measurements and the reasoning: dev/changelog/1209 (detection) and 1210 (the
restart). On ffmpeg 7.1 a feed whose video stops while its audio flows grows the segment
file one 256 KiB block at a time - at 64 kbps audio the blocks landed 22s and 27s apart,
under the 30s stall timeout - so the growth check never fires and the capture records sound
with no picture. The progress line's `frame=` count is what stops moving, and that is what
is watched; a fresh connection is what brought the picture back on recording 76.

No network anywhere: the stand-in capture is a local python child writing ffmpeg-shaped
progress lines into a real stderr spool and appending to a real segment file, all under
make_test_app's temp dir. The capture-command and join tests run real ffmpeg on a
synthesized local file.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.recorder as recorder  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    CAPTURE_VIDEO_RESUMED, CAPTURE_VIDEO_STOPPED, RESTART_SUCCEEDED, STALL_DETECTED, Recording,
    RecordingEvent, RecordingSegment,
)
from app.proc_utils import (  # noqa: E402
    VideoArrivalMonitor, build_capture_cmd, read_capture_frame_count,
)
from app.recorder import RecordingState  # noqa: E402
from app.watchdog import WatchdogThread, decide_video_stop  # noqa: E402
from tests.test_re_pacing import STREAM, _fake_proc, _real_config  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

# Verbatim from the measuring runs in dev/changelog/1209: ffmpeg 7.1 under a stream copy,
# and ffmpeg 6.1 - recording 76's version - which prints no frame count at all.
LINE_71 = ('frame=  748 fps= 30 q=-1.0 size=    5888KiB time=00:00:29.91 '
           'bitrate=1612.4kbits/s speed=0.98x    \r')
LINE_61 = 'size= 1655296kB time=01:06:43.35 bitrate=3387.2kbits/s speed=   1x\n'


class FrameCountTests(unittest.TestCase):
    """Reading the video frame count out of a spool ffmpeg is still writing."""

    def setUp(self):
        self.t = make_test_app()
        self.path = os.path.join(self.t._tmpdir, 'spool.log')

    def tearDown(self):
        self.t.cleanup()

    def _write(self, text):
        with open(self.path, 'w') as fh:
            fh.write(text)

    def test_reads_the_newest_count(self):
        self._write(LINE_71.replace('748', '700') + LINE_71)
        self.assertEqual(read_capture_frame_count(self.path), 748)

    def test_a_number_cut_off_mid_write_falls_back_to_the_last_complete_one(self):
        """`frame=  74` cut from `frame=  748` would read as the counter going backwards."""
        self._write(LINE_71 + 'frame=  74')
        self.assertEqual(read_capture_frame_count(self.path), 748)

    def test_ffmpeg_61_progress_has_no_opinion(self):
        """6.1 prints no frame count under a copy, so the detector is off - not "no video"."""
        self._write(LINE_61 * 3)
        self.assertIsNone(read_capture_frame_count(self.path))

    def test_a_missing_spool_has_no_opinion(self):
        self.assertIsNone(read_capture_frame_count(os.path.join(self.t._tmpdir, 'nope.log')))
        self.assertIsNone(read_capture_frame_count(None))


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class VideoArrivalMonitorTests(unittest.TestCase):

    def setUp(self):
        self.clock = _Clock()
        self.m = VideoArrivalMonitor(30, clock=self.clock)

    def _feed(self, frames, size, at):
        self.clock.now = at
        return self.m.update(frames, size)

    def test_frames_still_and_file_growing_past_the_threshold_is_stopped(self):
        self.assertIsNone(self._feed(100, 1000, 0))
        self.assertIsNone(self._feed(100, 1000, 1))
        self.assertIsNone(self._feed(100, 2000, 20))
        self.assertIsNone(self._feed(100, 2000, 31), 'not past the threshold yet')
        self.assertEqual(self._feed(100, 2000, 31.5), 'stopped')

    def test_a_feed_gone_silent_altogether_is_not_called_video_stopped(self):
        """Nothing growing is a stall, which GrowthMonitor owns. Reporting it as "video
        stopped, audio still arriving" a moment before the stall would claim audio that
        is not there."""
        for at in (0, 1, 10, 31, 60, 120):
            self.assertIsNone(self._feed(100, 1000, at))

    def test_a_late_block_still_reports_it(self):
        """Low-rate audio may not land its first block until well past the threshold."""
        self._feed(100, 1000, 0)
        self._feed(100, 1000, 1)
        self.assertIsNone(self._feed(100, 1000, 40))
        self.assertEqual(self._feed(100, 1500, 45), 'stopped')

    def test_reported_once_per_stretch(self):
        self._feed(100, 1000, 0)
        self._feed(100, 1000, 1)
        self.assertEqual(self._feed(100, 2000, 40), 'stopped')
        self.assertIsNone(self._feed(100, 3000, 60))
        self.assertIsNone(self._feed(100, 4000, 90))

    def test_resumed_says_how_long(self):
        self._feed(100, 1000, 0)
        self._feed(100, 1000, 1)
        self._feed(100, 2000, 40)
        self.assertEqual(self._feed(130, 2500, 61), 'resumed')
        self.assertAlmostEqual(self.m.stopped_seconds, 60.0)
        self.assertFalse(self.m.stopped)

    def test_moving_frames_never_report(self):
        for i, at in enumerate(range(0, 200, 5)):
            self.assertIsNone(self._feed(i * 150, i * 1000, at))

    def test_an_unreadable_sample_changes_nothing(self):
        self._feed(100, 1000, 0)
        self._feed(100, 1000, 1)
        self.assertIsNone(self._feed(None, 9000, 40))
        self.assertEqual(self._feed(100, 2000, 41), 'stopped')


# A stand-in capture: argv = video_seconds freeze_seconds grow seg_path with_frame.
# Counts frames for video_seconds, holds the count for freeze_seconds (-1 = for good), then
# counts again; appends to the segment file every tick when grow is 1 - the audio that
# keeps arriving. with_frame 0 writes ffmpeg 6.1's shape, which carries no frame count.
_FAKE_CAPTURE = (
    'import sys, time\n'
    'video_s, freeze_s = float(sys.argv[1]), float(sys.argv[2])\n'
    'grow, seg, with_frame = int(sys.argv[3]), sys.argv[4], int(sys.argv[5])\n'
    'start = time.monotonic()\n'
    'frames, content = 0, 0.0\n'
    'while True:\n'
    '    el = time.monotonic() - start\n'
    '    frozen = el >= video_s and (freeze_s < 0 or el < video_s + freeze_s)\n'
    '    if not frozen:\n'
    '        frames += 3\n'
    '    content += 0.1\n'
    '    if grow:\n'
    '        with open(seg, "ab") as fh:\n'
    '            fh.write(b"\\x47" * 188)\n'
    '    head = ("frame=%5d fps= 30 q=-1.0 " % frames) if with_frame else ""\n'
    '    sys.stderr.write(head + "size=    1024KiB time=00:00:%05.2f bitrate=1.0kbits/s '
    'speed=1x    \\r" % (content % 60))\n'
    '    sys.stderr.flush()\n'
    '    time.sleep(0.1)\n'
)


class WatchdogVideoArrivalTests(unittest.TestCase):
    """The whole path, driven by the real WatchdogThread against a live spool."""

    STALL_TIMEOUT = 1

    def setUp(self):
        self.t = make_test_app()
        self.wd = None
        self.launched = []
        self.procs = []
        self.failover = None
        self.state = RecordingState(current_segment_num=0)
        now = datetime.utcnow()
        self.account = seed.make_account()
        self.ch = seed.make_channel(self.account, stream_id='1', name='Member A')
        db.session.commit()
        rec = seed.make_recording(status='IN_PROGRESS', name='wdva', channel_id=self.ch.id,
                                  started_at=now, start_time=now,
                                  stop_time=now + timedelta(hours=1))
        self.rid = rec.id
        self.seg_path = self._seg_file(0)
        db.session.add(RecordingSegment(recording_id=self.rid, segment_number=0,
                                        channel_id=self.ch.id, file_path=self.seg_path,
                                        started_at=now))
        db.session.commit()
        with recorder._lock:
            recorder._active[self.rid] = self.state

    def tearDown(self):
        self.state.stop_event.set()
        if self.wd is not None:
            self.wd.join(timeout=15)
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        if self.state.stderr_fh is not None:
            self.state.stderr_fh.close()
        with recorder._lock:
            recorder._active.pop(self.rid, None)
        self.t.cleanup()

    def _seg_file(self, seg_num):
        path = os.path.join(self.t._tmpdir, f'rec_{self.rid}_seg_{seg_num:03d}.ts')
        with open(path, 'wb') as fh:
            fh.write(b'\x47' * 4096)
        return path

    def _cfg(self, budget):
        return cfgmod._deep_merge(cfgmod.load_config(), {'watchdog': {
            'poll_interval_seconds': 0.2,
            'stall_timeout_seconds': self.STALL_TIMEOUT,
            'restart_delay_seconds': 0,
            'max_consecutive_failures': 99,
            'early_fail_abort_count': 99,
            'stall_move_count': 0,
            'placeholder_content_ratio': 0,
            'fast_delivery_ratio': 0,
            'video_stop_restart_count': budget,
            'video_stop_window_minutes': 30,
        }})

    def _spawn(self, seg_num, seg_path, video_s, freeze_s, grow, with_frame):
        path, fh = recorder._open_segment_stderr_spool(self.t.app, self.rid, seg_num)
        self.state.stderr_path, self.state.stderr_fh = path, fh
        proc = subprocess.Popen(
            [sys.executable, '-c', _FAKE_CAPTURE, str(video_s), str(freeze_s), str(grow),
             seg_path, str(with_frame)],
            stdout=subprocess.DEVNULL, stderr=(fh or subprocess.DEVNULL))
        self.procs.append(proc)
        self.state.process = proc

    def _run(self, video_s, freeze_s, grow, with_frame=1, seconds=None, budget=3,
             failover=False, relaunch=None):
        """Run for `seconds`, or until segment 0 is closed and a relaunch was asked for
        when seconds is None. `relaunch` = (video_s, freeze_s): the stubbed launch starts
        a real stand-in capture for the next segment, so the watchdog can watch it too."""
        self._spawn(0, self.seg_path, video_s, freeze_s, grow, with_frame)

        def _stub_launch(app, recording_id, seg_num):
            self.launched.append(seg_num)
            if relaunch is None:
                return recorder.LAUNCH_SPAWNED
            seg_path = self._seg_file(seg_num)
            db.session.add(RecordingSegment(recording_id=recording_id, segment_number=seg_num,
                                            channel_id=self.ch.id, file_path=seg_path,
                                            started_at=datetime.utcnow()))
            db.session.commit()
            self._spawn(seg_num, seg_path, relaunch[0], relaunch[1], 1, 1)
            self.state.current_segment_num = seg_num
            return recorder.LAUNCH_SPAWNED

        self.failover = mock.MagicMock(return_value=failover)
        with mock.patch.object(cfgmod, 'load_config', return_value=self._cfg(budget)), \
             mock.patch('app.watchdog.wait_for_file_data', return_value=True), \
             mock.patch.object(recorder, '_launch_segment', _stub_launch), \
             mock.patch.object(recorder, 'failover_group_member', self.failover):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            deadline = time.monotonic() + (seconds or 30)
            while time.monotonic() < deadline:
                if seconds is None:
                    db.session.expire_all()
                    seg = RecordingSegment.query.filter_by(
                        recording_id=self.rid, segment_number=0).first()
                    if seg.ended_at is not None and self.launched:
                        break
                time.sleep(0.1)
            self.state.stop_event.set()
            self.wd.join(timeout=15)
        db.session.expire_all()
        return RecordingSegment.query.filter_by(recording_id=self.rid, segment_number=0).first()

    def _events(self, event_type):
        return RecordingEvent.query.filter_by(recording_id=self.rid,
                                              event_type=event_type).all()

    # One stop-then-resume run read by three tests - the real watchdog has to watch the
    # threshold pass, so each run costs seconds (the one-expensive-fixture shape of
    # tests/test_fast_delivery_detection.py::_default_detection). Restarts are turned off
    # and the recording is a single channel, so the capture is left on its audio and the
    # picture comes back on the same segment.
    _stop_resume = None

    def _stop_resume_run(self):
        if WatchdogVideoArrivalTests._stop_resume is None:
            seg = self._run(video_s=0.5, freeze_s=2.5, grow=1, seconds=5, budget=0)
            WatchdogVideoArrivalTests._stop_resume = {
                'ended': seg.ended_at is not None,
                'stopped': [(e.segment_number, e.detail) for e in
                            self._events(CAPTURE_VIDEO_STOPPED)],
                'resumed': [(e.segment_number, e.detail) for e in
                            self._events(CAPTURE_VIDEO_RESUMED)],
                'stalls': len(self._events(STALL_DETECTED)),
            }
        return WatchdogVideoArrivalTests._stop_resume

    def test_video_stopping_while_the_file_grows_is_announced(self):
        run = self._stop_resume_run()
        self.assertEqual(len(run['stopped']), 1)
        seg_num, detail = run['stopped'][0]
        self.assertEqual(seg_num, 0)
        self.assertIn('Video stopped arriving on segment 0', detail)
        self.assertIn('kept growing', detail)

    def test_video_coming_back_is_announced_too(self):
        run = self._stop_resume_run()
        self.assertEqual(len(run['resumed']), 1)
        self.assertIn('Video resumed on segment 0', run['resumed'][0][1])

    def test_with_no_restart_allowed_and_nowhere_to_move_the_capture_keeps_its_sound(self):
        """The segment is not closed, nothing counts a stall, and the event says why."""
        run = self._stop_resume_run()
        self.assertFalse(run['ended'])
        self.assertEqual(run['stalls'], 0)
        self.assertIn('left running to keep the sound', run['stopped'][0][1])
        self.assertIn('single channel', run['stopped'][0][1])

    def test_a_feed_that_stops_altogether_is_a_stall_not_a_video_stop(self):
        """Frames and file both still: the growth check owns it, and claiming "audio still
        arriving" would be false."""
        seg = self._run(video_s=0.5, freeze_s=-1, grow=0)
        self.assertIsNotNone(seg.ended_at)
        self.assertEqual(len(self._events(STALL_DETECTED)), 1)
        self.assertEqual(self._events(CAPTURE_VIDEO_STOPPED), [])

    def test_no_frame_count_means_no_opinion(self):
        """ffmpeg 6.1, and any stream with no video, print no frame count - the detector
        stays off rather than reading the absence as "video stopped"."""
        seg = self._run(video_s=0.5, freeze_s=-1, grow=1, with_frame=0, seconds=4)
        self.assertIsNone(seg.ended_at)
        self.assertEqual(self._events(CAPTURE_VIDEO_STOPPED), [])

    # ── The restart (dev/changelog/1210) ──────────────────────────────────────────

    _restart = None

    def _restart_run(self):
        """Video stops for good on segment 0; the restart's segment 1 carries video."""
        if WatchdogVideoArrivalTests._restart is None:
            seg = self._run(video_s=0.5, freeze_s=-1, grow=1, relaunch=(99, 0), seconds=6)
            rec = db.session.get(Recording, self.rid)
            WatchdogVideoArrivalTests._restart = {
                'exit_reason': seg.exit_reason,
                'stall_count': seg.stall_count,
                'launched': list(self.launched),
                'stopped': [e.detail for e in self._events(CAPTURE_VIDEO_STOPPED)],
                'resumed': [(e.segment_number, e.detail) for e in
                            self._events(CAPTURE_VIDEO_RESUMED)],
                'stalls': len(self._events(STALL_DETECTED)),
                'restart_succeeded': len(self._events(RESTART_SUCCEEDED)),
                'total_stall_count': rec.total_stall_count,
                'consecutive_failures_peak': rec.consecutive_failures_peak,
                'total_restart_count': rec.total_restart_count,
            }
        return WatchdogVideoArrivalTests._restart

    def test_video_stopping_restarts_the_capture(self):
        """The CAPTURE_VIDEO_STOPPED report alone left recording 76's fault running until
        the stop time with no picture; a fresh connection is what brought it back."""
        run = self._restart_run()
        self.assertEqual(run['exit_reason'], 'VIDEO_STOPPED')
        self.assertEqual(run['launched'], [1])
        self.assertEqual(run['restart_succeeded'], 1)
        self.assertEqual(run['total_restart_count'], 1)
        self.assertIn('Restarting the capture to get the picture back (restart 1 of 3',
                      run['stopped'][0])

    def test_a_video_stop_restart_is_never_a_stall(self):
        """Not a stall and not a failure: the file never stopped growing, and a consecutive
        failure here would let max_consecutive_failures fail a recording that still had its
        sound."""
        run = self._restart_run()
        self.assertEqual(run['stalls'], 0)
        self.assertEqual(run['total_stall_count'], 0)
        self.assertEqual(run['consecutive_failures_peak'], 0)
        self.assertFalse(run['stall_count'])

    def test_the_restart_that_brings_the_picture_back_says_so(self):
        run = self._restart_run()
        self.assertEqual(len(run['resumed']), 1)
        seg_num, detail = run['resumed'][0]
        self.assertEqual(seg_num, 1)
        self.assertIn('Video is back on segment 1', detail)
        self.assertIn('stopped on segment 0', detail)

    def test_past_the_budget_a_group_recording_moves_as_a_demotion(self):
        """Demoted, not burned: the member is still sending its sound."""
        db.session.get(Recording, self.rid).group_id = seed.make_group(members=[self.ch]).id
        db.session.commit()
        seg = self._run(video_s=0.5, freeze_s=-1, grow=1, budget=0, failover=True)
        self.assertEqual(seg.exit_reason, 'VIDEO_STOPPED')
        self.assertEqual(self.launched, [1])
        self.assertTrue(self.failover.called)
        self.assertTrue(self.failover.call_args.kwargs.get('demote'))
        self.assertIn('moved to another member of its group',
                      self._events(CAPTURE_VIDEO_STOPPED)[0].detail)
        self.assertEqual(self._events(STALL_DETECTED), [])

    def test_a_move_nobody_can_take_keeps_the_sound(self):
        db.session.get(Recording, self.rid).group_id = seed.make_group(members=[self.ch]).id
        db.session.commit()
        seg = self._run(video_s=0.5, freeze_s=-1, grow=1, budget=0, failover=False,
                        seconds=4)
        self.assertIsNone(seg.ended_at)
        self.assertEqual(self.launched, [])
        self.assertIn('no other member of its group could take it',
                      self._events(CAPTURE_VIDEO_STOPPED)[0].detail)

    def test_a_declared_video_track_that_never_carries_a_frame_is_not_video_stopping(self):
        """Under keep_video_track a radio mount that declares a video PID and never fills it
        prints frame=0 forever. Until the recording has carried video at all, that is not a
        picture that stopped, and restarting it every stall timeout would be the harm."""
        seg = self._run(video_s=0, freeze_s=-1, grow=1, seconds=4)
        self.assertIsNone(seg.ended_at)
        self.assertEqual(self.launched, [])
        self.assertEqual(self._events(CAPTURE_VIDEO_STOPPED), [])

    def test_a_restart_into_still_dead_video_is_noticed_again(self):
        """After a service restart, or on a segment opened while the video is still out,
        the frame count stands at 0 from the start - the recording's earlier probed picture
        is what says video is expected."""
        db.session.get(RecordingSegment,
                       RecordingSegment.query.filter_by(recording_id=self.rid).first().id
                       ).probe_resolution = '1920x1080'
        db.session.commit()
        seg = self._run(video_s=0, freeze_s=-1, grow=1)
        self.assertEqual(seg.exit_reason, 'VIDEO_STOPPED')
        self.assertEqual(self.launched, [1])


class DecideVideoStopTests(unittest.TestCase):
    """The per-member budget, as a rolling window (dev/changelog/1210)."""

    def test_restarts_while_the_window_has_room(self):
        action, times, retry_at = decide_video_stop([100.0], 200.0, 1800, 3, False)
        self.assertEqual(action, 'restart')
        self.assertEqual(times, [100.0, 200.0])
        self.assertIsNone(retry_at)

    def test_past_the_budget_a_group_moves_and_a_single_channel_stays(self):
        times = [100.0, 200.0, 300.0]
        self.assertEqual(decide_video_stop(times, 400.0, 1800, 3, True)[0], 'move')
        action, kept, retry_at = decide_video_stop(times, 400.0, 1800, 3, False)
        self.assertEqual(action, 'stay')
        self.assertEqual(kept, times)
        self.assertEqual(retry_at, 100.0 + 1800 + 1, 'asks again once the oldest ages out')

    def test_the_window_frees_a_restart_again(self):
        """A picture that drops once an hour across a long recording is restarted every
        time - a total would stop restarting it after the third."""
        action, times, _ = decide_video_stop([100.0, 200.0, 300.0], 100.0 + 1801, 1800, 3,
                                             False)
        self.assertEqual(action, 'restart')
        self.assertEqual(times, [200.0, 300.0, 1901.0])

    def test_a_zero_budget_never_restarts_and_never_asks_again(self):
        action, times, retry_at = decide_video_stop([], 50.0, 1800, 0, False)
        self.assertEqual((action, times, retry_at), ('stay', [], None))


def _ffmpeg_ready():
    return shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None


@unittest.skipUnless(_ffmpeg_ready(), 'needs ffmpeg and ffprobe')
class KeepVideoTrackTests(unittest.TestCase):
    """A segment opened while the feed's video is out must keep its video track, or the
    join puts that segment's sound into the video track (dev/docs/BUGS.md 2026-10-16 05:00 PM,
    dev/changelog/1210). Measured on
    7.1 with a real capture, and reproduced here on a synthesized local file: a stream
    whose PMT declares a video PID that carries no packets."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix='keepvideo-')
        cls.src = os.path.join(cls.dir, 'src.ts')
        subprocess.run(['ffmpeg', '-v', 'error', '-y',
                        '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=25',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                        '-t', '4', '-c:v', 'mpeg2video', '-c:a', 'mp2',
                        '-f', 'mpegts', cls.src], check=True, timeout=60)
        with open(cls.src, 'rb') as fh:
            data = fh.read()
        cls.dead = os.path.join(cls.dir, 'dead.ts')
        with open(cls.dead, 'wb') as fh:
            for i in range(0, len(data), 188):
                pkt = data[i:i + 188]
                if (((pkt[1] & 0x1f) << 8) | pkt[2]) != 0x100:  # the video PID
                    fh.write(pkt)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _capture(self, name, keep):
        out = os.path.join(self.dir, name)
        cfg = {'ffmpeg': {'path': 'ffmpeg', 'extra_input_args': [],
                          'extra_output_args': []}}
        cmd = build_capture_cmd(cfg, self.dead, out, pace_realtime=False,
                                keep_video_track=keep)
        subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, check=True,
                       timeout=60)
        return out

    def _streams(self, path):
        out = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', path],
                             capture_output=True, text=True, check=True, timeout=60).stdout
        return [st['codec_type'] for st in json.loads(out)['streams']]

    def _audio_packets(self, path):
        return int(subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'a', '-count_packets',
             '-show_entries', 'stream=nb_read_packets', '-of', 'csv=p=0', path],
            capture_output=True, text=True, check=True, timeout=60).stdout.split()[0])

    def _join(self, middle, name):
        listing = os.path.join(self.dir, name + '.txt')
        with open(listing, 'w') as fh:
            for path in (self.src, middle, self.src):
                fh.write(f"file '{path}'\n")
        out = os.path.join(self.dir, name)
        # The join's own argv - app/concatenator.py::_run_concatenation.
        subprocess.run(['ffmpeg', '-v', 'quiet', '-err_detect', 'ignore_err',
                        '-fflags', '+genpts+discardcorrupt', '-f', 'concat', '-safe', '0',
                        '-i', listing, '-c', 'copy', '-y', out],
                       capture_output=True, check=True, timeout=60)
        return out

    def test_the_capture_keeps_an_empty_video_track(self):
        self.assertEqual(self._streams(self._capture('kept.ts', True)), ['video', 'audio'])

    def test_the_join_keeps_every_segments_sound(self):
        expected = 2 * self._audio_packets(self.src) + self._audio_packets(self.dead)
        joined = self._join(self._capture('kept-mid.ts', True), 'joined.ts')
        self.assertEqual(self._audio_packets(joined), expected)

    def test_the_default_selection_is_what_drops_it(self):
        """Characterization of the ffmpeg behavior the flag exists for - this is what a
        test capture still gets, where an audio-only output is how "no picture" shows."""
        self.assertEqual(self._streams(self._capture('dropped.ts', False)), ['audio'])

    def test_a_users_own_map_is_left_alone(self):
        cfg = {'ffmpeg': {'path': 'ffmpeg', 'extra_input_args': [],
                          'extra_output_args': ['-map', '0']}}
        cmd = build_capture_cmd(cfg, 'http://example.test/live/1', '/out.ts',
                                keep_video_track=True)
        self.assertEqual(cmd.count('-map'), 1)


class RecorderKeepsVideoTrackTests(unittest.TestCase):
    """The recorder is the caller that passes keep_video_track; read off the real argv."""

    def setUp(self):
        self.t = make_test_app()
        acct = seed.make_account(name='Keep Acct')
        self.channel = seed.make_channel(acct, stream_id=91, name='Keep Channel')
        self.channel.stream_url = self.channel.raw_stream_url = STREAM
        self.rec = seed.make_recording(status='IN_PROGRESS', channel_id=self.channel.id,
                                       url=STREAM)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_a_recording_segment_maps_video_and_audio_explicitly(self):
        with mock.patch('app.recorder.load_config') as load_cfg:
            cfg = _real_config()
            cfg['recording']['dvr_output_dir'] = self.t._tmpdir
            cfg['ffmpeg']['extra_output_args'] = []
            load_cfg.return_value = cfg
            with mock.patch('app.recorder.subprocess.Popen',
                            return_value=_fake_proc()) as popen:
                recorder._launch_segment(self.t.app, self.rec.id, seg_num=1)
        argv = popen.call_args[0][0]
        maps = [argv[i + 1] for i, a in enumerate(argv) if a == '-map']
        self.assertEqual(maps, ['0:v?', '0:a?'])


class DetailPageTests(unittest.TestCase):
    """The recording's own page is where this is disclosed - no alert (dev/changelog/928)."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_stopped_renders_as_a_warning_and_resumed_as_ok(self):
        from app.database import add_recording_event
        rec = seed.make_recording(status='COMPLETED', name='wdva-page')
        add_recording_event(rec.id, CAPTURE_VIDEO_STOPPED, detail='stopped-detail',
                            segment_number=1, extra={'frames': 748, 'seconds': 30.5})
        add_recording_event(rec.id, CAPTURE_VIDEO_RESUMED, detail='resumed-detail',
                            segment_number=1, extra={'frames': 760, 'seconds': 45.0})
        db.session.commit()
        resp = self.t.app.test_client().get(f'/recordings/{rec.id}')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        for ev_type, cls in ((CAPTURE_VIDEO_STOPPED, 'warn'), (CAPTURE_VIDEO_RESUMED, 'ok')):
            li = html[:html.index(f'<span class="ev-type">{ev_type}</span>')]
            self.assertTrue(li[li.rindex('<li class="ev '):].startswith(f'<li class="ev {cls}"'),
                            ev_type)
        self.assertIn('stopped-detail', html)

    def test_a_video_stop_restart_is_its_own_segment_ending(self):
        """Named for what happened, not shown as a stall or an unknown reason, and its
        restart is not given a second boundary marker beside the Video stopped milestone."""
        from app.database import add_recording_event
        account = seed.make_account()
        ch = seed.make_channel(account, stream_id='5', name='Pill Channel')
        start = datetime.utcnow() - timedelta(minutes=10)
        rec = seed.make_recording(status='COMPLETED', name='wdva-pill', channel_id=ch.id,
                                  started_at=start, start_time=start)
        seed.make_segment(rec, ch, start, start + timedelta(minutes=4),
                          exit_reason='VIDEO_STOPPED')
        seed.make_segment(rec, ch, start + timedelta(minutes=4), start + timedelta(minutes=9),
                          segment_number=1, exit_reason='STOP_TIME_REACHED')
        add_recording_event(rec.id, CAPTURE_VIDEO_STOPPED, detail='stopped-detail',
                            segment_number=0, extra={'frames': 748, 'seconds': 30.5})
        db.session.commit()
        html = self.t.app.test_client().get(f'/recordings/{rec.id}').get_data(as_text=True)
        self.assertIn('the sound kept coming', html)
        self.assertIn('Video stopped', html)
        self.assertNotIn('Segment boundary', html)


if __name__ == '__main__':
    unittest.main()
