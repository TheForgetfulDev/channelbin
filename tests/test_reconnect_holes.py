"""The join closes the holes ffmpeg's reconnect repair leaves in a segment's timeline
(app/hole_repair.py, dev/changelog/1414).

Guards dev/docs/BUGS.md 2026-11-02: a segment whose capture reported a hole - ffmpeg closed
the provider's replay jump once per stream after an in-process reconnect - was joined with
the hole in it, so the file froze for as long as the replay at a point where no content was
missing. The hole is built here the way a real cut makes it: the loop's bytes cut inside a
video PES and an audio PES, then the loop again from a keyframe a few seconds earlier, read
by ffmpeg exactly as the recorder's own stream copy reads a reconnected stream.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import concatenator, db  # noqa: E402
from app.database import DIAGNOSTICS, Recording, RecordingEvent, RecordingSegment  # noqa: E402
from app.hole_repair import (DIAGNOSTIC_KIND, HoleMatch, HoleRepair,  # noqa: E402
                             close_reconnect_holes, match_reconnect_holes, setts_args)
from app.probe import scan_timeline_jumps  # noqa: E402
from app.proc_utils import TIMELINE_GAP_KEEP_SECONDS  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))

SECONDS = 14
CUT_AT = 9.0
REPLAY = 4.0


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True, timeout=180)


def _loop(path):
    """A synthetic channel: picture and sound, one keyframe a second, B-frames so the video
    PES order is a real encoder's. Noise rather than a tone, so each AAC frame is a real
    frame's size and its PES spans more than one TS packet, as a channel's does - a cut can
    only land inside a PES that has a second packet. The seed is fixed because the noise decides
    where the cut lands: unseeded, 8 of 360 builds cut where ffmpeg leaves a second audio jump
    and the fixture was not the one hole these tests are about (dev/docs/BUGS.md 2026-11-04)."""
    _ffmpeg('-f', 'lavfi', '-i', f'testsrc2=size=192x108:rate=25:duration={SECONDS}',
            '-f', 'lavfi', '-i', f'anoisesrc=amplitude=0.5:duration={SECONDS}:seed=1437',
            '-c:v', 'libx264', '-preset', 'veryfast', '-g', '25', '-bf', '2',
            '-c:a', 'aac', '-f', 'mpegts', path)


def _ts_packets(data):
    """[(pid, payload unit start, payload bytes)] for every 188-byte packet."""
    out = []
    for i in range(0, len(data) - len(data) % 188, 188):
        b = data[i:i + 188]
        pid = ((b[1] & 0x1f) << 8) | b[2]
        pusi = bool(b[1] & 0x40)
        afc = (b[3] >> 4) & 3
        off = 4 + (1 + b[4] if afc & 2 else 0)
        out.append((pid, pusi, b[off:] if afc & 1 else b''))
    return out


def _packets(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                          'packet=stream_index,pts_time,dts_time,flags,pos', '-of', 'json',
                          path], capture_output=True, text=True, timeout=120).stdout
    return json.loads(out)['packets']


def _cut_and_replay(loop, dest, cut_at=CUT_AT, replay=REPLAY):
    """`dest` is `loop` cut after `cut_at` seconds inside both a video and an audio PES, then
    `loop` again from the keyframe at or before `cut_at - replay`: the bytes a reconnected
    capture reads when the provider answers from its buffer."""
    with open(loop, 'rb') as fh:
        data = fh.read()
    pk = _ts_packets(data)
    video = [p for p in _packets(loop) if p['stream_index'] == 0 and 'pos' in p]
    vpid = pk[int(video[0]['pos']) // 188][0]
    # The other PES stream: a PES starts with the 00 00 01 start code, a table does not.
    apid = next(pid for pid, pusi, payload in pk
                if pusi and pid != vpid and payload.startswith(b'\x00\x00\x01'))
    keyframes = [(float(p['dts_time']), int(p['pos']) // 188) for p in video
                 if 'K' in p['flags']]
    start = max(idx for d, idx in keyframes if d <= cut_at - replay)
    after = next(int(p['pos']) // 188 for p in video if float(p['dts_time']) >= cut_at)
    acc, need = {}, {}
    cut = None
    for i, (pid, pusi, payload) in enumerate(pk[:-1]):
        if pid in (vpid, apid):
            if pusi:
                length = (payload[4] << 8) | payload[5]
                need[pid] = length + 6 if length else float('inf')
                acc[pid] = len(payload)
            else:
                acc[pid] = acc.get(pid, 0) + len(payload)
        if i <= after:
            continue
        nxt = pk[i + 1]
        video_open = acc.get(vpid, 0) < need.get(vpid, 0) and not (nxt[0] == vpid and nxt[1])
        audio_open = acc.get(apid, 0) < need.get(apid, 0)
        if video_open and audio_open:
            cut = i
            break
    if cut is None:
        raise AssertionError('no cut lands inside a video PES and an audio PES at once')
    with open(dest, 'wb') as fh:
        fh.write(data[:(cut + 1) * 188])
        fh.write(data[start * 188:])


def _capture(source, dest):
    """What the recorder writes: ffmpeg's stream copy of the bytes, its timestamp repair
    included."""
    _ffmpeg('-dts_delta_threshold', str(TIMELINE_GAP_KEEP_SECONDS), '-i', source,
            '-map', '0:v', '-map', '0:a', '-c', 'copy', '-f', 'mpegts', dest)


def _hole_of(path):
    """The one hole the file's video shows, in seconds, or None."""
    jumps = scan_timeline_jumps(path).get(0, {}).get('jumps', [])
    if len(jumps) != 1:
        return None
    prev, dts, cadence = jumps[0]
    return round(dts - prev - cadence, 3)


def _head(path):
    """(size, first 4 KB): enough to tell a file left alone from one rewritten."""
    with open(path, 'rb') as fh:
        return os.path.getsize(path), fh.read(4096)


def _counts(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-count_packets', '-show_entries',
                          'stream=index,nb_read_packets', '-of', 'csv=p=0', path],
                         capture_output=True, text=True, timeout=120).stdout
    return {int(line.split(',')[0]): int(line.split(',')[1])
            for line in out.splitlines() if line.strip()}


class MatchTests(unittest.TestCase):
    """Pure: which jumps are the reported holes."""

    def scan(self, video, audio):
        return {0: {'packets': 100, 'jumps': video}, 1: {'packets': 200, 'jumps': audio}}

    def test_a_hole_on_every_stream_is_matched_at_its_midpoint_by_its_excess(self):
        matches, left = match_reconnect_holes(
            self.scan([(28.64, 33.821, 0.04)], [(28.424, 33.685, 0.0213)]), [5.141])
        self.assertEqual(left, [])
        self.assertEqual(len(matches), 1)
        self.assertAlmostEqual(matches[0].shifts[0][0], 31.2305, places=3)
        self.assertAlmostEqual(matches[0].shifts[0][1], 5.141, places=3)
        self.assertAlmostEqual(matches[0].shifts[1][1], 5.2397, places=3)

    def test_a_hole_one_stream_does_not_show_stays_on_every_stream(self):
        matches, left = match_reconnect_holes(self.scan([(10, 15.04, 0.04)], []), [5.0])
        self.assertEqual(matches, [])
        self.assertEqual(len(left), 1)
        self.assertIn('stream 1 shows no jump of about 5.0 s', left[0][1])

    def test_a_jump_no_reconnect_explains_is_not_closed(self):
        matches, left = match_reconnect_holes(
            self.scan([(10, 22.04, 0.04)], [(10, 22.02, 0.02)]), [5.0])
        self.assertEqual(matches, [])
        self.assertEqual(len(left), 1)

    def test_holes_are_matched_in_file_order(self):
        matches, left = match_reconnect_holes(
            self.scan([(10, 14.04, 0.04), (30, 36.04, 0.04)],
                      [(10, 14.02, 0.02), (30, 36.02, 0.02)]), [4.0, 6.0])
        self.assertEqual([m.reported for m in matches], [4.0, 6.0])
        self.assertEqual(left, [])
        matches, left = match_reconnect_holes(
            self.scan([(10, 14.04, 0.04), (30, 36.04, 0.04)],
                      [(10, 14.02, 0.02), (30, 36.02, 0.02)]), [6.0, 4.0])
        self.assertEqual([m.reported for m in matches], [6.0])
        self.assertEqual([h for h, _ in left], [4.0])

    def test_a_real_gap_of_the_holes_size_elsewhere_is_not_taken_for_it(self):
        """A video-only gap as long as the hole, earlier in the file, matches on the video
        alone; the hole is one place in the file, so nothing is closed."""
        matches, left = match_reconnect_holes(
            self.scan([(10, 15.04, 0.04), (40, 45.04, 0.04)], [(40, 45.02, 0.02)]), [5.0])
        self.assertEqual(matches, [])
        self.assertEqual(len(left), 1)
        self.assertIn('near 12.5 s', left[0][1])
        # and a hole one stream lacks moves no cursor on the other
        matches, left = match_reconnect_holes(
            self.scan([(10, 15.04, 0.04), (40, 46.04, 0.04)], [(40, 46.02, 0.02)]),
            [5.0, 6.0])
        self.assertEqual([m.reported for m in matches], [6.0])

    def test_a_single_packet_stream_has_no_say(self):
        scan = {0: {'packets': 100, 'jumps': [(10, 15.04, 0.04)]},
                2: {'packets': 1, 'jumps': []}}
        matches, left = match_reconnect_holes(scan, [5.0])
        self.assertEqual(len(matches), 1)
        self.assertEqual(set(matches[0].shifts), {0})

    def test_setts_moves_every_packet_after_each_boundary_by_the_holes_before_it(self):
        args = setts_args([HoleMatch(5.0, {0: (31.0, 5.1), 1: (31.0, 5.2)}),
                           HoleMatch(6.0, {0: (60.0, 6.0), 1: (60.0, 6.1)})])
        self.assertEqual(args[0], '-bsf:0')
        self.assertEqual(args[1], 'setts=pts=PTS-5.100000/TB*gte(DTS*TB\\,31.000000)'
                                  '-6.000000/TB*gte(DTS*TB\\,60.000000):dts=DTS'
                                  '-5.100000/TB*gte(DTS*TB\\,31.000000)'
                                  '-6.000000/TB*gte(DTS*TB\\,60.000000)')
        self.assertEqual(args[2], '-bsf:1')
        self.assertIn('-5.200000/TB', args[3])


FIX = {}


def setUpModule():
    """The fixture once for the module, whatever order the classes run in."""
    if not _HAVE_FFMPEG:
        return
    src = tempfile.mkdtemp(prefix='reconnect-holes-')
    FIX['src'] = src
    loop = os.path.join(src, 'loop.ts')
    _loop(loop)
    cut = os.path.join(src, 'cut.ts')
    _cut_and_replay(loop, cut)
    FIX['holed'] = os.path.join(src, 'holed.ts')
    _capture(cut, FIX['holed'])
    FIX['hole'] = _hole_of(FIX['holed'])
    if FIX['hole'] is None or not 1.0 < FIX['hole'] < 10.0:
        raise AssertionError(f'the fixture shows no one hole: {FIX["hole"]} in '
                             f'{scan_timeline_jumps(FIX["holed"])}')
    FIX['clean'] = os.path.join(src, 'clean.ts')
    _capture(loop, FIX['clean'])


def tearDownModule():
    if FIX.get('src'):
        shutil.rmtree(FIX['src'], True)


@unittest.skipUnless(_HAVE_FFMPEG, 'needs ffmpeg and ffprobe')
class RepairTests(unittest.TestCase):
    """Real ffmpeg over a file with the hole in it."""

    @classmethod
    def setUpClass(cls):
        cls.holed, cls.clean, cls.hole = FIX['holed'], FIX['clean'], FIX['hole']

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix='reconnect-holes-work-')
        self.addCleanup(shutil.rmtree, self.work, True)
        self.path = os.path.join(self.work, 'seg.ts')
        shutil.copyfile(self.holed, self.path)

    def repair(self, path, holes):
        return close_reconnect_holes(path, holes, ffmpeg_path='ffmpeg',
                                     pre_output_timeout=60, stall_seconds=60)

    def test_the_fixture_has_the_hole_on_both_streams_and_nothing_missing(self):
        scan = scan_timeline_jumps(self.holed)
        self.assertEqual(len(scan[0]['jumps']), 1, scan)
        self.assertEqual(len(scan[1]['jumps']), 1, scan)
        self.assertAlmostEqual(self.hole, REPLAY, delta=1.5)

    def test_the_hole_is_closed_in_place_with_every_packet_kept(self):
        before = _counts(self.path)
        repair = self.repair(self.path, [self.hole])
        self.assertIsNone(repair.error, repair)
        self.assertEqual(len(repair.closed), 1, repair)
        self.assertAlmostEqual(repair.closed[0], self.hole, delta=0.2)
        self.assertEqual(repair.left, [])
        after = scan_timeline_jumps(self.path)
        self.assertEqual(after[0]['jumps'], [], after)
        self.assertEqual(after[1]['jumps'], [], after)
        self.assertEqual(_counts(self.path), before)
        self.assertFalse(os.path.exists(self.path + '.holes.ts'))

    def test_the_streams_stay_in_step_after_the_close(self):
        self.repair(self.path, [self.hole])
        scan = scan_timeline_jumps(self.path)
        self.assertAlmostEqual(scan[0]['last_dts'], scan[1]['last_dts'], delta=0.2)

    def test_a_reported_hole_the_file_does_not_show_leaves_the_file_alone(self):
        shutil.copyfile(self.clean, self.path)
        stamp = _head(self.path)
        repair = self.repair(self.path, [self.hole])
        self.assertIsNone(repair.error)
        self.assertEqual(repair.closed, [])
        self.assertEqual(len(repair.left), 1)
        self.assertTrue(repair.scanned)
        self.assertEqual(_head(self.path), stamp)

    def test_a_remux_that_reads_as_different_content_is_refused(self):
        stamp = _head(self.path)
        with mock.patch('app.hole_repair.remux_args',
                        lambda ffmpeg_path, source, dest, matches: [
                            ffmpeg_path, '-v', 'error', '-i', source, '-t', '3', '-map', '0',
                            '-c', 'copy', '-f', 'mpegts', '-y', dest]):
            repair = self.repair(self.path, [self.hole])
        self.assertIsNotNone(repair.error, repair)
        self.assertEqual(repair.closed, [])
        self.assertEqual(_head(self.path), stamp)
        self.assertFalse(os.path.exists(self.path + '.holes.ts'))

    def test_nothing_reported_scans_nothing(self):
        with mock.patch('app.probe.scan_timeline_jumps') as scan:
            repair = self.repair(self.path, [])
        self.assertEqual(repair, HoleRepair([], [], None, False))
        scan.assert_not_called()


@unittest.skipUnless(_HAVE_FFMPEG, 'needs ffmpeg and ffprobe')
class JoinTests(unittest.TestCase):
    """The repair through the real join, driven by what the capture reported."""

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)

    def _recording(self, *clips, holes=None):
        now = datetime.utcnow()
        rec = seed.make_recording(status='CONCATENATING', name='holes',
                                  start_time=now - timedelta(seconds=30), stop_time=now)
        db.session.flush()
        for n, clip in enumerate(clips, 1):
            path = os.path.join(self.dvr, f'holes_{rec.id}_seg_{n:03d}.ts')
            shutil.copyfile(clip, path)
            db.session.add(RecordingSegment(
                recording_id=rec.id, segment_number=n, file_path=path, started_at=now,
                ended_at=now + timedelta(seconds=14), bytes_recorded=os.path.getsize(path)))
            if holes and n in holes:
                db.session.add(RecordingEvent(
                    recording_id=rec.id, event_type=DIAGNOSTICS, segment_number=n,
                    detail='capture ended',
                    extra_data=json.dumps({'kind': 'capture_stderr',
                                           'reconnect_replay_seconds': holes[n],
                                           'reconnect_hole_seconds': holes[n]})))
        db.session.commit()
        return rec.id

    def _join(self, rid):
        with mock.patch('app.postprocessor.do_postprocess') as post, \
                mock.patch('app.recorder.persist_final_thumbnail'), \
                mock.patch('app.recorder.persist_poster_frame'):
            concatenator._run_concatenation(self.t.app, rid, reason='test')
        db.session.expire_all()
        self.assertEqual(post.call_count, 1, self._details(rid))
        return db.session.get(Recording, rid)

    def _details(self, rid):
        return [(e.event_type, e.segment_number, e.detail) for e in
                RecordingEvent.query.filter_by(recording_id=rid).all()]

    def _repair_events(self, rid):
        return [e for e in RecordingEvent.query.filter_by(recording_id=rid,
                                                          event_type=DIAGNOSTICS).all()
                if DIAGNOSTIC_KIND in (e.extra_data or '')]

    def test_a_reported_hole_is_closed_before_the_join_and_said(self):
        hole = FIX['hole']
        rid = self._recording(FIX['holed'], FIX['clean'], holes={1: [hole]})
        rec = self._join(rid)
        scan = scan_timeline_jumps(rec.output_path)
        self.assertEqual(scan[0]['jumps'], [], scan)
        events = self._repair_events(rid)
        self.assertEqual(len(events), 1, self._details(rid))
        self.assertEqual(events[0].segment_number, 1)
        self.assertIn('the join closed the 1 hole of about', events[0].detail)
        self.assertIn('plays straight through', events[0].detail)
        extra = json.loads(events[0].extra_data)
        self.assertAlmostEqual(extra['closed_seconds'][0], hole, delta=0.2)
        self.assertEqual(extra['left_seconds'], [])
        self.assertEqual(extra['outcome'], 'closed')

    def test_a_single_segment_recording_is_repaired_too(self):
        rid = self._recording(FIX['holed'], holes={1: [FIX['hole']]})
        rec = self._join(rid)
        self.assertEqual(scan_timeline_jumps(rec.output_path)[0]['jumps'], [])
        self.assertEqual(len(self._repair_events(rid)), 1)

    def test_a_hole_the_file_does_not_show_is_reported_and_the_file_joined_as_it_is(self):
        rid = self._recording(FIX['clean'], FIX['clean'], holes={2: [5.0]})
        self._join(rid)
        events = self._repair_events(rid)
        self.assertEqual(len(events), 1, self._details(rid))
        self.assertEqual(events[0].segment_number, 2)
        self.assertIn('was reported but', events[0].detail)
        self.assertIn('so it stays', events[0].detail)
        self.assertEqual(json.loads(events[0].extra_data)['left_seconds'], [5.0])

    def test_a_segment_with_no_reported_hole_is_not_scanned(self):
        rid = self._recording(FIX['clean'], FIX['clean'])
        with mock.patch('app.hole_repair.close_reconnect_holes') as close:
            self._join(rid)
        close.assert_not_called()
        self.assertEqual(self._repair_events(rid), [])

    def test_a_retry_join_does_not_look_again(self):
        rid = self._recording(FIX['holed'], holes={1: [FIX['hole']]})
        rec = self._join(rid)
        rec.output_path = None
        db.session.commit()
        seg = RecordingSegment.query.filter_by(recording_id=rid).one()
        shutil.copyfile(FIX['holed'], seg.file_path)
        with mock.patch('app.hole_repair.close_reconnect_holes') as close:
            with mock.patch('app.postprocessor.do_postprocess'), \
                    mock.patch('app.recorder.persist_final_thumbnail'), \
                    mock.patch('app.recorder.persist_poster_frame'), \
                    mock.patch('app.concatenator.committed_concat_output', return_value=None):
                concatenator._run_concatenation(self.t.app, rid, reason='retry')
        close.assert_not_called()
        self.assertEqual(len(self._repair_events(rid)), 1)

    def test_a_repair_that_fails_names_why_and_joins_the_file_as_it_was(self):
        rid = self._recording(FIX['holed'], FIX['clean'], holes={1: [FIX['hole']]})
        with mock.patch('app.hole_repair.remux_args',
                        lambda ffmpeg_path, source, dest, matches: [
                            ffmpeg_path, '-v', 'error', '-i', source, '-t', '3', '-map', '0',
                            '-c', 'copy', '-f', 'mpegts', '-y', dest]):
            rec = self._join(rid)
        events = self._repair_events(rid)
        self.assertEqual(len(events), 1, self._details(rid))
        self.assertIn('could not close', events[0].detail)
        self.assertIn('left as it was', events[0].detail)
        self.assertEqual(json.loads(events[0].extra_data)['outcome'], 'error')
        self.assertEqual(len(scan_timeline_jumps(rec.output_path)[0]['jumps']), 1)
        # A failed repair is tried again on a Retry join.
        reported, done = concatenator._reported_reconnect_holes(rid)
        self.assertEqual(done, set())


if __name__ == '__main__':
    unittest.main()
