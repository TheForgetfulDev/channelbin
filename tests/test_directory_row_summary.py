"""The "N listings, T titles" summary of an EPG directory row has one spelling
(dev/changelog/1244).

The channel page's Listings cell, the Set key dialog's result lines and the review page's
file cell each rendered the same four fields of an EpgSourceChannel row in their own words,
with their own `>= 20` cap test and their own single-title rule - the cap passed to one of
the three and hard-coded in the other two. epg_sources.directory_row_summary() is the
sentence, carried as `summary` by every row view; a sole title is rendered after it.

  python3 -m unittest tests.test_directory_row_summary
"""
import os
import unittest

from app.epg_sources import DISTINCT_TITLE_CAP, directory_row_summary

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


class SummaryTests(unittest.TestCase):
    def test_the_three_shapes(self):
        self.assertEqual(directory_row_summary(0, 0), 'no listings')
        self.assertEqual(directory_row_summary(1234, 1), '1,234 listings, one program all day')
        self.assertEqual(directory_row_summary(1234, 7), '1,234 listings, 7 titles')

    def test_the_cap_is_the_one_constant(self):
        self.assertEqual(directory_row_summary(500, DISTINCT_TITLE_CAP),
                         f'500 listings, {DISTINCT_TITLE_CAP}+ titles')
        self.assertEqual(directory_row_summary(500, DISTINCT_TITLE_CAP - 1),
                         f'500 listings, {DISTINCT_TITLE_CAP - 1} titles')

    def test_nulls_read_as_nothing(self):
        self.assertEqual(directory_row_summary(None, None), 'no listings')

    def test_every_row_view_carries_it_and_no_surface_builds_its_own(self):
        epg = _read('app/epg_sources.py')
        self.assertEqual(epg.count("'summary': directory_row_summary("), 2)
        self.assertIn("'summary': directory_row_summary(", _read('app/routes/accounts.py'))
        self.assertNotIn('title_cap', _read('templates/channels/detail.html'))
        self.assertNotIn("'+' if f.distinct_titles >= 20", _read('templates/epg_source_review.html'))
        self.assertNotIn('epgTitleCap', _read('static/js/channel-detail.js'))
        self.assertIn('r.summary', _read('static/js/channel-detail.js'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
