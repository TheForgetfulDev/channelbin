"""What a first-time user meets on Channel Search and the guide's way into it
(dev/changelog/1276, findings 22 and 24 of the October 2026 first-time-user test).

Each assertion here is a thing a person who had never seen the app could not explain: a mark
beside the filter headings that drew as a blank box, two tags it never made described as
labels "you put on a channel yourself", a guide button that opened a list hiding every
channel it could add, and a filter named "Other".
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app.channel_search import DIMENSION_BY_KEY  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as f:
        return f.read()


class InfoMarkerTests(unittest.TestCase):

    def test_the_info_marker_is_not_a_font_glyph(self):
        """U+24D8 is in no font on a stock Linux box, so the browser drew a blank box that
        read as a stray grey bar. The ring is CSS around a plain "i" (dev/docs/BUGS.md
        2026-10-23 08:28)."""
        js = _read('static/js/channel-search.js')
        self.assertNotIn('&#9432;', js)
        self.assertNotIn('ⓘ', js)
        self.assertGreater(len(re.findall(r'class="fwhat"[^>]*>i</span>', js)), 0)
        css = _read('static/css/channel-search.css')
        self.assertRegex(css, r'(?m)^\.fwhat \{[^}]*border-radius: 999px')


class FilterWordingTests(unittest.TestCase):

    def test_the_tag_help_owns_up_to_the_two_it_ships_with(self):
        help_text = DIMENSION_BY_KEY['tag'].help
        self.assertIn('live and new', help_text)
        self.assertIn('Tags page', help_text)
        self.assertNotIn('you put on a channel yourself', help_text)

    def test_the_grab_bag_filter_has_a_name(self):
        self.assertEqual('Status', DIMENSION_BY_KEY['other'].label)
        self.assertEqual('other', DIMENSION_BY_KEY['other'].key,
                         'the URL key is a contract; only the label moved')


class ManageChannelsTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        seed.make_channel(seed.make_account(), name='Test Channel', in_guide=True)

    def tearDown(self):
        self.t.cleanup()

    def test_manage_channels_opens_every_channel(self):
        """Pre-filtered to the guide's own channels, it hid every channel a user could add
        until they found and removed the chip."""
        html = self.t.client.get('/guide').get_data(as_text=True)
        m = re.search(r'<a href="([^"]*)"[^>]*>Manage channels</a>', html)
        self.assertIsNotNone(m, 'the guide lost its Manage channels button')
        self.assertNotIn('f.other', m.group(1))


if __name__ == '__main__':
    unittest.main()
