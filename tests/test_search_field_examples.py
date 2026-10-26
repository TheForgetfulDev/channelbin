"""The Search-in pane's Example column, read off the user's own channels.

The examples used to be fixed strings from one developer's provider ("FS2 (720p)",
"WORLD CUP 2026", "foxsports2.us"), and a first-time user read them as channels they were
meant to have (dev/docs/UX-first-time-user-2026-10-retest.md finding 22, dev/changelog/1290).
These pin that an example is a real value from one channel the user is offered, or nothing.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.channel_search import FIELDS, field_examples  # noqa: E402

CATALOG_URL = '/api/channels/search/catalog'


class FieldExampleTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def hints(self):
        resp = self.t.client.get(CATALOG_URL)
        self.assertEqual(resp.status_code, 200)
        return {f['key']: f['hint'] for f in resp.get_json()['fields']}

    def test_an_install_with_no_channels_shows_no_examples(self):
        self.assertEqual(set(self.hints().values()), {''})

    def test_a_guide_channel_is_the_example_over_an_earlier_one(self):
        acct = seed.make_account('Alpha')
        seed.make_channel(acct, stream_id=11, name='Earlier Channel',
                          category_name='Earlier Cat')
        seed.make_channel(acct, stream_id=22, name='Guide Channel', in_guide=True,
                          category_name='News', epg_channel_id='guide.example')
        db.session.commit()
        hints = self.hints()
        self.assertEqual(hints['name'], 'Guide Channel')
        self.assertEqual(hints['cat'], 'News')
        self.assertEqual(hints['tvg'], 'guide.example')
        self.assertEqual(hints['sid'], '22')
        self.assertEqual(hints['url'], '22')

    def test_a_hidden_channel_is_never_the_example(self):
        acct = seed.make_account('Alpha')
        seed.make_channel(acct, stream_id=5, name='Hidden One', in_guide=True, hidden=True)
        seed.make_channel(acct, stream_id=6, name='Offered One')
        db.session.commit()
        self.assertEqual(self.hints()['name'], 'Offered One')

    def test_the_url_example_is_given_only_when_the_url_holds_the_stream_id(self):
        """A URL's path can carry the login or a token, so the URL field's example is the
        stream id and only when the URL really contains it - never a piece of the URL."""
        acct = seed.make_account('Alpha')
        ch = seed.make_channel(acct, stream_id=77, name='Token Channel')
        ch.stream_url = 'http://example.test/play/SECRETTOKEN'
        db.session.commit()
        hints = self.hints()
        self.assertEqual(hints['url'], '')
        self.assertEqual(hints['sid'], '77')
        self.assertNotIn('SECRETTOKEN', ''.join(hints.values()))

    def test_an_empty_value_gives_no_example_for_that_field_alone(self):
        acct = seed.make_account('Alpha')
        seed.make_channel(acct, stream_id=3, name='No Category', epg_channel_id=None)
        db.session.commit()
        ex = field_examples()
        self.assertEqual(ex.get('name'), 'No Category')
        self.assertNotIn('cat', ex)
        self.assertNotIn('tvg', ex)

    def test_program_fields_never_carry_an_example(self):
        acct = seed.make_account('Alpha')
        seed.make_channel(acct, stream_id=4, name='Any')
        db.session.commit()
        hints = self.hints()
        for f in FIELDS:
            if f.source != 'channel':
                self.assertEqual(hints[f.key], '', f.key)


if __name__ == '__main__':
    unittest.main()
