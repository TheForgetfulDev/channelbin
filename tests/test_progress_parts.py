"""The join and analysis progress sentences are built beside their registries
(dev/changelog/1244).

The recordings list and the Dashboard each turned concat_progress() and
analysis_progress() into prose on their own - the percentage, "N of M segments", the pass
label with "(n of m)" - so a change to what a registry reports had two consumers to keep in
step. concatenator.concat_progress_parts() and postprocessor.analysis_progress_parts()
return the ordered parts; each page frames them.

  python3 -m unittest tests.test_progress_parts
"""
import os
import unittest

from app.concatenator import concat_progress_parts
from app.postprocessor import analysis_progress_parts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConcatPartsTests(unittest.TestCase):
    def test_percentage_then_count(self):
        self.assertEqual(concat_progress_parts({'pct': 42.6, 'joined': 3, 'of': 7}),
                         ['43%', '3 of 7 segments'])

    def test_no_percentage_yet_is_just_the_count(self):
        self.assertEqual(concat_progress_parts({'pct': None, 'joined': 0, 'of': 7}),
                         ['0 of 7 segments'])


class AnalysisPartsTests(unittest.TestCase):
    def test_single_pass_label_then_percentage(self):
        self.assertEqual(analysis_progress_parts(
            {'pass_label': 'reading the timeline', 'pass_number': 1, 'of_passes': 1, 'pct': 10.0}),
            ['reading the timeline', '10%'])

    def test_a_multi_pass_run_numbers_the_pass(self):
        self.assertEqual(analysis_progress_parts(
            {'pass_label': 'looking for black', 'pass_number': 2, 'of_passes': 2, 'pct': None}),
            ['looking for black (2 of 2)'])


class OneSpellingTests(unittest.TestCase):
    def test_neither_page_builds_the_parts_itself(self):
        for rel in ('app/routes/recordings.py', 'app/routes/dashboard.py'):
            with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
                src = fh.read()
            self.assertNotIn("of {prog['of']} segments", src, rel)
            self.assertNotIn("of {concat_prog['of']} segments", src, rel)
            self.assertNotIn("['of_passes'] > 1", src, rel)
            self.assertIn('concat_progress_parts(', src, rel)
            self.assertIn('analysis_progress_parts(', src, rel)


if __name__ == '__main__':
    unittest.main(verbosity=2)
