"""Tier 2 - the recording's format pin follows what its segment SETTLED at, not only what
it opened at (dev/changelog/1308).

`recorder.recording_format_pin()` reads the earliest probed segment's capture-time probe,
which sees its first two megabytes. dev/changelog/1201 found the provider's feed dropping
to 720p29.97 for a minute or two during trouble; had segment 1 opened in such a stretch the
pin would have been 720p30 for the whole recording and every failover would have preferred
720p members. The watchdog now re-reads the pin segment's TAIL once it has run a while,
records it as the settled format, and the pin prefers that reading.

The fixture is a real MPEG-TS this box's ffmpeg wrote: three seconds of 320x180 @ 30 joined
to twenty of 640x360 @ 60, the way the concatenator joins segments. The recheck interval and
tail size are patched down from their production values; the probe itself is the real
ffprobe with -skip_initial_bytes, which is what the first class measures.

No network: the child here is `sys.executable -c ...` (tests/support/watchdog.py::sleeper).
"""
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
import app.watchdog as wdmod  # noqa: E402
from app import db  # noqa: E402
from app.database import RECORDING_FORMAT_CHANGED, RecordingEvent, RecordingSegment  # noqa: E402
from app.probe import parse_ffprobe  # noqa: E402
from app.recorder import recording_format_pin  # noqa: E402
from app.watchdog import WatchdogThread  # noqa: E402
from tests.support.watchdog import WatchdogHarness, sleeper  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))

OPENING = ('320x180', 30)
SETTLED = ('640x360', 60)


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True, timeout=120)


def _mixed_clip(workdir):
    """open30.ts (3 s at 30) + rest60.ts (20 s at 60), concat-joined like segments are."""
    enc = ['-c:v', 'libx264', '-preset', 'ultrafast', '-bf', '0', '-pix_fmt', 'yuv420p']
    first = os.path.join(workdir, 'open30.ts')
    rest = os.path.join(workdir, 'rest60.ts')
    _ffmpeg('-f', 'lavfi', '-i', f'testsrc=size={OPENING[0]}:rate={OPENING[1]}', '-t', '3',
            *enc, first)
    _ffmpeg('-f', 'lavfi', '-i', f'testsrc=size={SETTLED[0]}:rate={SETTLED[1]}', '-t', '20',
            *enc, rest)
    listfile = os.path.join(workdir, 'list.txt')
    with open(listfile, 'w') as fh:
        fh.write(f"file '{first}'\nfile '{rest}'\n")
    out = os.path.join(workdir, 'mixed.ts')
    _ffmpeg('-f', 'concat', '-safe', '0', '-fflags', '+genpts', '-i', listfile, '-c', 'copy', out)
    return out


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class TailProbeTests(unittest.TestCase):
    """parse_ffprobe(skip_bytes=...) reads the format in force at that offset - measured,
    since the whole recheck rests on it."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='pinrecheck-')
        cls.clip = _mixed_clip(cls._dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def test_the_head_reads_the_opening_and_the_tail_the_settled_format(self):
        size = os.path.getsize(self.clip)
        head = parse_ffprobe(self.clip, count_packets=False, timeout=15)
        tail = parse_ffprobe(self.clip, count_packets=False, timeout=15,
                             skip_bytes=size - 512 * 1024)
        self.assertEqual(head['resolution'], OPENING[0])
        self.assertEqual(tail['resolution'], SETTLED[0])
        self.assertAlmostEqual(tail['fps'], SETTLED[1], places=1)


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class SettledFormatRecheckTests(WatchdogHarness):
    """The watchdog's re-read, on a real WatchdogThread over the fixture."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(prefix='pinrecheck-wd-')
        cls.clip = _mixed_clip(cls._dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def setUp(self):
        super().setUp()
        shutil.copyfile(self.clip, self.seg_path)
        self.state.process = sleeper()

    def _probed(self, seg_num=0, key=OPENING, minutes_ago=0):
        seg = RecordingSegment.query.filter_by(recording_id=self.rid,
                                               segment_number=seg_num).first()
        seg.probe_resolution, seg.probe_fps = key[0], float(key[1])
        seg.probed_at = datetime.utcnow() - timedelta(minutes=minutes_ago)
        db.session.commit()
        return seg.id

    def _run(self, seconds=12.0, recheck_seconds=1, tail_bytes=256 * 1024):
        """A watchdog over the open segment with the recheck a second away and the tail
        a quarter megabyte, so the fixture's 880 KB is twice the tail."""
        deadline = time.monotonic() + seconds
        with mock.patch.object(cfgmod, 'load_config', return_value=self._cfg(60)), \
             mock.patch.object(recorder, '_launch_segment', self._stub_launch()), \
             mock.patch.object(wdmod, '_PIN_RECHECK_SECONDS', recheck_seconds), \
             mock.patch.object(wdmod, '_SETTLED_TAIL_BYTES', tail_bytes):
            self.wd = WatchdogThread(self.rid, self.state, self.t.app)
            self.wd.start()
            while time.monotonic() < deadline:
                db.session.expire_all()
                seg = RecordingSegment.query.filter_by(recording_id=self.rid,
                                                       segment_number=0).first()
                if seg.probe_settled_at is not None:
                    break
                time.sleep(0.2)
            self.state.stop_event.set()
            self.wd.join(timeout=15)
        db.session.expire_all()
        return RecordingSegment.query.filter_by(recording_id=self.rid, segment_number=0).first()

    def _changes(self):
        return RecordingEvent.query.filter_by(recording_id=self.rid,
                                              event_type=RECORDING_FORMAT_CHANGED).all()

    def test_the_pin_segment_is_re_read_and_the_pin_follows_it(self):
        self._probed()
        seg = self._run()
        self.assertEqual(seg.probe_settled_resolution, SETTLED[0])
        self.assertAlmostEqual(seg.probe_settled_fps, SETTLED[1], places=1)
        self.assertEqual((seg.probe_resolution, seg.probe_fps), (OPENING[0], 30.0),
                         'the opening reading must survive the re-read')
        self.assertEqual(recording_format_pin(self.rid), SETTLED)

    def test_a_pin_that_moved_is_said_on_the_recording(self):
        self._probed()
        self._run()
        [ev] = self._changes()
        self.assertIn('opened at 320x180 @ 30', ev.detail)
        self.assertIn('carrying 640x360 @ 60', ev.detail)
        self.assertIn('format pin follows the later reading', ev.detail)
        self.assertEqual(ev.segment_number, 0)

    def test_a_settled_format_equal_to_the_opening_is_recorded_quietly(self):
        self._probed(key=SETTLED)
        seg = self._run()
        self.assertEqual(seg.probe_settled_resolution, SETTLED[0])
        self.assertEqual(self._changes(), [])
        self.assertEqual(recording_format_pin(self.rid), SETTLED)

    def test_a_segment_that_is_not_the_pins_is_not_re_read(self):
        """An earlier segment holds the pin; the current one's settled format moves nothing,
        so it is not read."""
        earlier = RecordingSegment(recording_id=self.rid, segment_number=-1,
                                   file_path=self.seg_path + '.prev',
                                   started_at=datetime.utcnow() - timedelta(minutes=10),
                                   ended_at=datetime.utcnow() - timedelta(minutes=5),
                                   bytes_recorded=4096,
                                   probe_resolution=OPENING[0], probe_fps=30.0)
        db.session.add(earlier)
        db.session.commit()
        self._probed()
        seg = self._run(seconds=4.0)
        self.assertIsNone(seg.probe_settled_at)
        self.assertEqual(recording_format_pin(self.rid), OPENING)

    def test_a_file_still_too_small_for_a_tail_is_left_for_later(self):
        """Under twice the tail size the "tail" is the opening again, which would record
        the opening as settled and close the question wrongly."""
        self._probed()
        seg = self._run(seconds=4.0, tail_bytes=4 * 1024 * 1024)
        self.assertIsNone(seg.probe_settled_at)
        self.assertEqual(self._changes(), [])

    def test_a_segment_probed_before_this_watchdog_started_is_still_re_read(self):
        """A service restart mid-recording: the opening probe is already on the row, so
        the recheck is armed from it rather than waiting for a probe that will not come."""
        self._probed(minutes_ago=30)
        seg = self._run()
        self.assertEqual(seg.probe_settled_resolution, SETTLED[0])


if __name__ == '__main__':
    unittest.main()
