"""A supervised run's scratch prefix is vocabulary, not a bare string (dev/changelog/1244).

proc_utils.supervise_ffmpeg() writes its per-attempt -progress and stderr files under a
`scratch_prefix`, and two reapers match them: the function's own stale reap on the next
attempt, and recorder.recording_disk_paths() on a delete. The four prefixes and the two
filename shapes were re-typed in the recorder, so a fifth caller would have been reaped by
the first and left on disk forever by the second. Now SCRATCH_PREFIXES is the one list,
scratch_file_paths() the one enumeration, and an unknown prefix is refused before anything
is spawned.

  python3 -m unittest tests.test_scratch_prefixes
"""
import os
import unittest

from app import db
from app.proc_utils import SCRATCH_PREFIXES, scratch_file_paths, supervise_ffmpeg
from app.recorder import recording_disk_paths
from tests.support import seed
from tests.support.app import make_test_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class VocabularyTests(unittest.TestCase):
    def test_the_four_callers_prefixes_are_the_vocabulary(self):
        self.assertEqual(set(SCRATCH_PREFIXES), {'conv', 'concat', 'part', 'join'})

    def test_an_unknown_prefix_is_refused_before_a_spawn(self):
        with self.assertRaises(ValueError) as cm:
            supervise_ffmpeg(['ffmpeg'], os.path.join(ROOT, 'never-written.ts'),
                             scratch_prefix='bogus', scratch_key=1, interval=1,
                             pre_output_timeout=1, stall_seconds=1)
        self.assertIn('SCRATCH_PREFIXES', str(cm.exception))

    def test_enumeration_is_per_key_and_covers_both_shapes(self):
        d = os.path.join(ROOT, 'x')
        got = set(scratch_file_paths(d, 'conv', 6))
        self.assertIn(os.path.join(d, '.conv-progress-6.txt'), got)
        self.assertIn(os.path.join(d, '.conv-stderr-6.log'), got)

    def test_the_recorder_no_longer_retypes_the_list(self):
        with open(os.path.join(ROOT, 'app', 'recorder.py'), encoding='utf-8') as fh:
            src = fh.read()
        self.assertNotIn("('conv', 'concat', 'part', 'join')", src)
        self.assertIn('for prefix in SCRATCH_PREFIXES', src)


class DeleteReapsEveryPrefixTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_recording_disk_paths_lists_a_leftover_of_every_prefix_and_no_neighbour(self):
        acct = seed.make_account()
        ch = seed.make_channel(acct)
        out_dir = os.path.join(self.t._tmpdir, 'out')
        os.makedirs(out_dir)
        rec = seed.make_recording(status='COMPLETED', channel_id=ch.id,
                                  output_path=os.path.join(out_dir, 'show.ts'))
        db.session.commit()
        mine, theirs = [], []
        for prefix in SCRATCH_PREFIXES:
            for shape in (f'.{prefix}-progress-{rec.id}-ab12.txt', f'.{prefix}-stderr-{rec.id}-ab12.log',
                          f'.{prefix}-progress-{rec.id}.txt'):
                mine.append(os.path.join(out_dir, shape))
            theirs.append(os.path.join(out_dir, f'.{prefix}-progress-{rec.id}4-ab12.txt'))
        for p in mine + theirs:
            with open(p, 'w') as fh:
                fh.write('x')
        listed = set(recording_disk_paths(rec.id, cfg={'recording': {'post_process': {'enabled': False}}}))
        for p in mine:
            self.assertIn(p, listed, p)
        for p in theirs:
            self.assertNotIn(p, listed, 'id 6 must not reap id 64')


if __name__ == '__main__':
    unittest.main(verbosity=2)
