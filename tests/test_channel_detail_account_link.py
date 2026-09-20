"""The channel detail page links its owning account rather than naming it in plain text.

Cross-entity references in this app are rendered with the shared `account_pill` macro so
they navigate as real links (dev/changelog/1046). The channel page named its account as
dead text, which was the one place most likely to prompt "and what is that account doing?".

Also pins the assumption the link rests on: deleting an account takes its channels with it,
so the page never has to render a channel whose account is gone. If that cascade is ever
loosened, url_for turns the missing account into a 500 and this is the test that says so.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402


class AccountPillTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account(name='Sports Pack')
        self.ch = seed.make_channel(self.acc, name='FS1 HD')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _html(self):
        resp = self.t.client.get(f'/channels/{self.ch.id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_header_links_the_owning_account(self):
        html = self._html()
        head = html.split('class="cd-meta"', 1)[1].split('</div>', 1)[0]
        self.assertIn(f'href="/accounts/{self.acc.id}"', head)

    def test_account_name_is_rendered_through_the_shared_pill_macro(self):
        """The class is what keeps this one link styled like every other cross-entity
        reference - a hand-rolled <a> would drift from the rest of the app."""
        html = self._html()
        pill = re.search(
            r'<a class="ch-pill acct-pill[^>]*href="/accounts/%d"[^>]*>.*?Sports Pack'
            % self.acc.id, html, re.S)
        self.assertIsNotNone(pill, 'owning account not rendered as an acct-pill link')

    def test_pill_carries_the_account_color(self):
        """Scoped to the pill's own dot: the .acct-bar stripe further up the header
        paints the same color, so an unscoped search passes without the pill at all."""
        self.acc.color = '#ff8800'
        db.session.commit()
        html = self._html()
        dot = re.search(
            r'<a class="ch-pill acct-pill[^>]*>\s*<span class="acct-dot"[^>]*'
            r'background:\s*#ff8800', html)
        self.assertIsNotNone(dot, 'account pill did not carry the account color')

    def test_deleting_the_account_takes_the_channel_with_it(self):
        """The link is unguarded because this cascade holds - a channel outliving its
        account would reach url_for with nothing to build from."""
        db.session.delete(self.acc)
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(self.t.client.get(f'/channels/{self.ch.id}').status_code, 404)


if __name__ == '__main__':
    unittest.main()
