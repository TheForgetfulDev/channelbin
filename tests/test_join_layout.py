"""The join when a recording's segments do not share a stream layout (dev/changelog/1410).

Guards dev/docs/BUGS.md 2026-11-02: a segment with no video had its sound written into the
joined file's video track and the file was short that sound. Real ffmpeg over local clips, and
the real join through _run_concatenation; nothing here touches the network.
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
from app.database import (DIAGNOSTICS, Recording, RecordingEvent,  # noqa: E402
                          RecordingSegment)
from app.join_layout import (AUDIO, VIDEO, LayoutPlan, concat_stream_lines,  # noqa: E402
                             describe, plan_layouts, remux_args)
from app.probe import stream_layout  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

_HAVE_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))

BOTH = [(VIDEO, 0x100), (AUDIO, 0x101)]
SOUND = [(AUDIO, 0x100)]
PICTURE = [(VIDEO, 0x100)]
MB = 1024 * 1024

SECONDS = 2
VIDEO_PACKETS = 25 * SECONDS


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True, timeout=120)


def _clip(path, video=True, audio=True, frequency=440):
    args = []
    if video:
        args += ['-f', 'lavfi', '-i', f'testsrc2=size=192x108:rate=25:duration={SECONDS}']
    if audio:
        args += ['-f', 'lavfi', '-i', f'sine=frequency={frequency}:duration={SECONDS}']
    if video:
        args += ['-c:v', 'libx264', '-preset', 'ultrafast', '-g', '25']
    if audio:
        args += ['-c:a', 'aac']
    _ffmpeg(*args, '-f', 'mpegts', path)


def _packets(path, selector):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', selector, '-count_packets',
                          '-show_entries', 'stream=nb_read_packets,codec_name', '-of', 'json',
                          path], capture_output=True, text=True, timeout=60).stdout
    streams = json.loads(out).get('streams', [])
    return sum(int(s.get('nb_read_packets') or 0) for s in streams), [
        s.get('codec_name') for s in streams]


class PlanTests(unittest.TestCase):
    """Which layouts need anything at all."""

    def test_segments_that_agree_take_the_plain_path(self):
        self.assertEqual(plan_layouts([BOTH, BOTH, BOTH], [1, 1, 1]).tracks, ())

    def test_a_later_segment_lacking_a_trailing_track_lands_nowhere_wrong_and_is_named(self):
        plan = plan_layouts([BOTH, PICTURE], [1, 1])
        self.assertEqual(plan.tracks, ())
        self.assertEqual(plan.missing, {1: [AUDIO]})
        self.assertIn('Segment 8 had no audio track', describe(plan, {0: 7, 1: 8}, {}))

    def test_a_second_language_is_not_a_mismatch(self):
        two = BOTH + [(AUDIO, 0x102)]
        self.assertEqual(plan_layouts([BOTH, two], [1, 1]).tracks, ())
        self.assertEqual(plan_layouts([two, BOTH], [1, 1]).tracks, ())

    def test_a_segment_that_could_not_be_read_changes_nothing(self):
        self.assertEqual(plan_layouts([BOTH, None, BOTH], [1, 1, 1]), LayoutPlan())
        self.assertEqual(plan_layouts([None, None], [1, 1]), LayoutPlan())

    def test_a_segment_that_could_not_be_read_does_not_break_a_mismatch(self):
        """Found by review: the read-ahead walk took an unread segment's layout for a list of
        (kind, pid) pairs and raised, which would have killed the join's thread."""
        for layouts in ([None, SOUND, BOTH], [SOUND, None, BOTH], [None, BOTH, SOUND]):
            with self.subTest(layouts=layouts):
                plan = plan_layouts(layouts, [MB, MB, MB])
                self.assertEqual(plan.tracks, (VIDEO, AUDIO))
                self.assertNotIn(layouts.index(None), plan.remux)

    def test_a_segment_with_no_streams_at_all_does_not_break_a_mismatch(self):
        plan = plan_layouts([[], SOUND, BOTH], [MB, MB, MB])
        self.assertEqual(plan.missing[0], [VIDEO, AUDIO])

    def test_sound_alone_in_the_middle_is_rewritten_to_the_audio_track(self):
        plan = plan_layouts([BOTH, SOUND, BOTH], [MB, MB, MB])
        self.assertEqual(plan.tracks, (VIDEO, AUDIO))
        self.assertEqual(plan.remux, {1: [AUDIO]})
        self.assertEqual(plan.missing, {1: [VIDEO]})
        self.assertEqual(plan.set_aside, ())
        self.assertIsNone(plan.probe_bytes)

    def test_a_first_segment_lacking_a_track_reads_ahead_past_it(self):
        plan = plan_layouts([SOUND, BOTH], [3 * MB, MB])
        self.assertEqual(plan.tracks, (VIDEO, AUDIO))
        self.assertEqual(plan.probe_bytes, 3 * MB + 32 * MB)

    def test_a_lead_too_big_to_hold_in_memory_is_set_aside(self):
        plan = plan_layouts([PICTURE, BOTH, BOTH], [10 * MB, MB, MB], max_lead_bytes=5 * MB)
        self.assertEqual(plan.set_aside, (0,))
        self.assertIsNone(plan.probe_bytes)

    def test_a_rewrite_keeps_a_gap_in_one_stream(self):
        """The rewrite reads with the capture's own gap bound, or a kept video-only gap is
        closed up out of step with the audio on the way through (dev/changelog/1415)."""
        from app.proc_utils import TIMELINE_GAP_KEEP_SECONDS
        args = remux_args([VIDEO, AUDIO], 'in.ts', 'out.ts')
        self.assertEqual(args[args.index('-dts_delta_threshold') + 1],
                         str(TIMELINE_GAP_KEEP_SECONDS))
        self.assertLess(args.index('-dts_delta_threshold'), args.index('-i'))

    def test_the_header_names_each_track_by_packet_id(self):
        self.assertEqual(concat_stream_lines((VIDEO, AUDIO)),
                         'ffconcat version 1.0\nstream\nexact_stream_id 0x100\n'
                         'stream\nexact_stream_id 0x101\n')


@unittest.skipUnless(_HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class RealJoinTests(unittest.TestCase):
    """The defect and its repair through the real join."""

    @classmethod
    def setUpClass(cls):
        cls.src = tempfile.mkdtemp(prefix='join-layout-')
        cls.addClassCleanup(shutil.rmtree, cls.src, True)
        _clip(os.path.join(cls.src, 'both.ts'))
        _clip(os.path.join(cls.src, 'sound.ts'), video=False, frequency=660)
        _clip(os.path.join(cls.src, 'picture.ts'), audio=False)
        cls.audio_per_clip = _packets(os.path.join(cls.src, 'both.ts'), 'a')[0]

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)

    def _join(self, *clips):
        now = datetime.utcnow()
        rec = seed.make_recording(status='CONCATENATING', name='layout',
                                  start_time=now - timedelta(seconds=9), stop_time=now)
        db.session.flush()
        for n, clip in enumerate(clips, 1):
            path = os.path.join(self.dvr, f'layout_{rec.id}_seg_{n:03d}.ts')
            shutil.copyfile(os.path.join(self.src, clip), path)
            db.session.add(RecordingSegment(
                recording_id=rec.id, segment_number=n, file_path=path, started_at=now,
                ended_at=now + timedelta(seconds=2), bytes_recorded=os.path.getsize(path)))
        db.session.commit()
        with mock.patch('app.postprocessor.do_postprocess') as post, \
                mock.patch('app.recorder.persist_final_thumbnail'), \
                mock.patch('app.recorder.persist_poster_frame'):
            concatenator._run_concatenation(self.t.app, rec.id, reason='test')
        db.session.expire_all()
        self.assertEqual(post.call_count, 1, self._details(rec.id))
        return db.session.get(Recording, rec.id)

    def _details(self, rid):
        return [(e.event_type, e.detail) for e in
                RecordingEvent.query.filter_by(recording_id=rid).all()]

    def _note(self, rid):
        return [e for e in RecordingEvent.query.filter_by(recording_id=rid,
                                                          event_type=DIAGNOSTICS).all()
                if 'join_layout' in (e.extra_data or '')]

    def _assert_all_in_their_tracks(self, rec, *, video, audio):
        self.assertEqual(_packets(rec.output_path, 'v')[0], video)
        self.assertEqual(_packets(rec.output_path, 'a')[0], audio)
        err = subprocess.run(['ffmpeg', '-v', 'error', '-i', rec.output_path, '-f', 'null', '-'],
                             capture_output=True, text=True, timeout=60).stderr
        self.assertNotIn('startcode', err, 'audio written as H.264')

    def test_sound_alone_between_two_whole_segments_keeps_all_its_sound(self):
        rec = self._join('both.ts', 'sound.ts', 'both.ts')
        self._assert_all_in_their_tracks(rec, video=2 * VIDEO_PACKETS,
                                         audio=3 * self.audio_per_clip)
        notes = self._note(rec.id)
        self.assertEqual(len(notes), 1)
        self.assertIn('Segment 2 had no video track', notes[0].detail)
        self.assertIn('no picture for that stretch', notes[0].detail)

    def test_sound_first_keeps_the_picture_that_follows_it(self):
        rec = self._join('sound.ts', 'both.ts')
        self._assert_all_in_their_tracks(rec, video=VIDEO_PACKETS,
                                         audio=2 * self.audio_per_clip)

    def test_picture_first_keeps_the_sound_that_follows_it(self):
        rec = self._join('picture.ts', 'both.ts')
        self._assert_all_in_their_tracks(rec, video=2 * VIDEO_PACKETS,
                                         audio=self.audio_per_clip)
        self.assertIn('Segment 1 had no audio track', self._note(rec.id)[0].detail)

    def test_segments_that_agree_write_no_note_and_no_list_directives(self):
        written = []
        real = concatenator.write_concat_list

        def spy(paths, **kw):
            written.append(kw.get('header', ''))
            return real(paths, **kw)
        with mock.patch('app.concatenator.write_concat_list', spy):
            rec = self._join('both.ts', 'both.ts')
        self.assertEqual(written, [''])
        self.assertEqual(self._note(rec.id), [])
        self._assert_all_in_their_tracks(rec, video=2 * VIDEO_PACKETS,
                                         audio=2 * self.audio_per_clip)

    def test_no_scratch_file_is_left_and_every_joined_segment_is_consumed(self):
        rec = self._join('both.ts', 'sound.ts', 'both.ts')
        self.assertEqual(sorted(os.listdir(self.dvr)), [os.path.basename(rec.output_path)])

    def test_a_rewrite_that_fails_keeps_that_segment_out_and_names_it(self):
        real = concatenator.remux_args

        def broken(kinds, source, dest):
            return real(kinds, source + '.nowhere', dest)
        with mock.patch('app.concatenator.remux_args', broken):
            rec = self._join('both.ts', 'sound.ts', 'both.ts')
        self._assert_all_in_their_tracks(rec, video=2 * VIDEO_PACKETS,
                                         audio=2 * self.audio_per_clip)
        kept = os.path.join(self.dvr, f'layout_{rec.id}_seg_002.ts')
        self.assertTrue(os.path.exists(kept), 'the one copy of that sound stays on disk')
        note = self._note(rec.id)[0]
        self.assertIn('Kept out of the join', note.detail)
        self.assertNotIn('had no video track', note.detail, 'that sound is not in the file at all')
        self.assertIn(kept, note.detail)
        seg = RecordingSegment.query.filter_by(recording_id=rec.id, segment_number=2).one()
        self.assertEqual(json.loads(note.extra_data)['kept_out'], [seg.id])
        self.assertEqual(sorted(n for n in os.listdir(self.dvr) if '.layout.ts' in n), [],
                         'the failed rewrite took its scratch file with it')

    def test_a_kept_out_segment_is_not_called_gone_on_the_finished_page(self):
        rec = self._join('both.ts', 'sound.ts', 'both.ts')
        rec.status = 'COMPLETED'
        db.session.commit()
        html = self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        all_three = html.count('no longer on disk')
        self.assertGreater(all_three, 0)
        self.assertEqual(all_three % 3, 0)

        with mock.patch('app.concatenator.remux_args',
                        lambda k, s, d: ['-v', 'error', '-i', s + '.nowhere', d]):
            other = self._join('both.ts', 'sound.ts', 'both.ts')
        other.status = 'COMPLETED'
        db.session.commit()
        html = self.t.client.get(f'/recordings/{other.id}').get_data(as_text=True)
        self.assertEqual(html.count('no longer on disk'), all_three * 2 // 3)

    def test_progress_counts_only_what_a_failed_rewrite_left_in_the_join(self):
        started = []
        real = concatenator._start_concat_progress

        def spy(recording_id, *, total_bytes, sizes):
            started.append((total_bytes, list(sizes)))
            return real(recording_id, total_bytes=total_bytes, sizes=sizes)
        with mock.patch('app.concatenator.remux_args',
                        lambda k, s, d: ['-v', 'error', '-i', s + '.nowhere', d]), \
                mock.patch('app.concatenator._start_concat_progress', spy):
            self._join('both.ts', 'sound.ts', 'both.ts')
        both = os.path.getsize(os.path.join(self.src, 'both.ts'))
        self.assertEqual(started, [(2 * both, [both, both])])

    def test_a_join_that_dies_after_its_rewrites_leaves_no_scratch_file(self):
        with mock.patch('app.concatenator.write_concat_list', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self._join('both.ts', 'sound.ts', 'both.ts')
        self.assertEqual([n for n in os.listdir(self.dvr) if '.layout.ts' in n], [])

    def test_stream_layout_reads_kinds_and_packet_ids(self):
        self.assertEqual(stream_layout(os.path.join(self.src, 'both.ts')), BOTH)
        self.assertEqual(stream_layout(os.path.join(self.src, 'sound.ts')), SOUND)
        self.assertIsNone(stream_layout(os.path.join(self.src, 'nowhere.ts')))


if __name__ == '__main__':
    unittest.main()
