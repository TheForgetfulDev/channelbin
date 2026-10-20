"""The concat-demuxer list file escapes a single quote, and has one writer
(dev/docs/BUGS.md 2026-10-20, dev/changelog/1244).

ffmpeg's concat demuxer reads `file '...'` with shell-style quoting, so a path holding an
apostrophe has to be written as `'\\''`. Two writers - the segment join and the part
assembly - each wrote the path bare, so on an install whose output directory carries one
(`/mnt/Pat's DVR/`) every multi-segment join and every resumed conversion failed at the
list parse, on a file that does not exist. Measured with ffmpeg 7.1: the bare line fails
with "Impossible to open .../Pats", the escaped one joins.

  python3 -m unittest tests.test_concat_list_quoting
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from app.proc_utils import concat_list_line, write_concat_list

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConcatListLineTests(unittest.TestCase):
    def test_a_plain_path_is_quoted(self):
        self.assertEqual(concat_list_line('/dvr/a.ts'), "file '/dvr/a.ts'\n")

    def test_an_apostrophe_closes_escapes_and_reopens_the_quote(self):
        self.assertEqual(concat_list_line("/mnt/Pat's DVR/a.ts"),
                         "file '/mnt/Pat'\\''s DVR/a.ts'\n")

    def test_write_concat_list_writes_every_path_in_order_through_the_same_line(self):
        path = write_concat_list(["/x/a'b.ts", '/x/c.ts'], prefix='test_concat_')
        try:
            with open(path) as fh:
                self.assertEqual(fh.read(), "file '/x/a'\\''b.ts'\nfile '/x/c.ts'\n")
            self.assertTrue(os.path.basename(path).startswith('test_concat_'))
        finally:
            os.unlink(path)

    def test_both_joins_write_their_list_through_the_one_writer(self):
        """A second `file '{...}'` spelling outside proc_utils is the duplicate this closed."""
        pattern = re.compile(r"""file '\{""")
        for rel in ('app/concatenator.py', 'app/postprocessor.py'):
            with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
                src = fh.read()
            self.assertIsNone(pattern.search(src), f'{rel} writes its own concat line')
            self.assertIn('write_concat_list(', src, rel)


@unittest.skipIf(shutil.which('ffmpeg') is None, 'ffmpeg not on PATH')
class FfmpegReadsTheEscapedListTests(unittest.TestCase):
    """The empirical half: the real demuxer on this machine joins two clips from a directory
    whose name carries an apostrophe only when the list is written through the helper."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='cb-concat-quote-')
        self.dir = os.path.join(self.base, "Pat's DVR")
        os.makedirs(self.dir)
        self.a = os.path.join(self.dir, 'a.ts')
        subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-f', 'lavfi',
                        '-i', 'testsrc=size=64x64:rate=5', '-t', '0.6',
                        '-c:v', 'mpeg2video', '-f', 'mpegts', '-y', self.a],
                       check=True, timeout=60)
        self.b = os.path.join(self.dir, 'b.ts')
        shutil.copyfile(self.a, self.b)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _join(self, list_path):
        out = os.path.join(self.dir, 'out.ts')
        proc = subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-f', 'concat',
                               '-safe', '0', '-i', list_path, '-c', 'copy', '-y', out],
                              capture_output=True, text=True, timeout=60)
        return proc.returncode, proc.stderr, out

    def test_the_helper_written_list_joins_and_a_bare_one_does_not(self):
        escaped = write_concat_list([self.a, self.b], prefix='cb-quote-')
        bare = os.path.join(self.base, 'bare.txt')
        with open(bare, 'w') as fh:
            fh.write(f"file '{self.a}'\nfile '{self.b}'\n")
        try:
            rc, err, out = self._join(escaped)
            self.assertEqual(rc, 0, err)
            self.assertGreater(os.path.getsize(out), os.path.getsize(self.a))
            rc, err, _ = self._join(bare)
            self.assertNotEqual(rc, 0, 'the bare line is the defect; if ffmpeg now accepts '
                                       'it, the escaping is still correct but no longer load-bearing')
        finally:
            os.unlink(escaped)


if __name__ == '__main__':
    unittest.main(verbosity=2)
