"""The channel page's Health card agrees with itself before, during and after a test, and
the page leads with the channel's name (dev/changelog/1289).

Guards dev/docs/BUGS.md 2026-10-26 (the "enroll it" line that ignored enrollment, an
in-progress test read as one that measured nothing, Test now clickable again mid-test).
"""
import os
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.tester import running_status as _running_status  # noqa: E402
from app import db  # noqa: E402


def _health_card(body):
    start = body.index('id="cd-health"')
    return body[start:body.index('data-section="settings"', start)]


def _test_now_buttons(body):
    return re.findall(r'<button[^>]*data-act="test-now"[^>]*>', body)


class HealthCardTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()

    def tearDown(self):
        self.t.cleanup()

    def _get(self, ch, status=None):
        status = status or {'is_running': False, 'current_channel_id': None}
        with patch('app.routes.channels.get_status', return_value=status):
            resp = self.t.client.get(f'/channels/{ch.id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_untested_guide_channel_names_the_check_that_tests_it(self):
        """dev/docs/BUGS.md 2026-10-26: right after Add to Guide the card still said "enroll
        it in a health check" - it never looked at enrollment. A guide channel is in the
        automatic check, and the card says so."""
        ch = seed.make_channel(self.acc, name='Guide Channel', in_guide=True)
        db.session.commit()
        card = _health_card(self._get(ch))
        self.assertNotIn('enroll it in', card)
        self.assertIn('TV Guide health check', card)

    def test_untested_channel_outside_every_check_says_so(self):
        ch = seed.make_channel(self.acc, name='Loose Channel')
        db.session.commit()
        card = _health_card(self._get(ch))
        self.assertNotIn('enroll it in', card)
        self.assertIn('No health check includes it', card)

    def test_test_in_progress_is_named_not_read_as_no_measurements(self):
        """dev/docs/BUGS.md 2026-10-26: while a test ran, the card read its FAILED placeholder
        row as "The last test produced no stream measurements" beside a "No screenshot ...
        during the last test" box."""
        ch = seed.make_channel(self.acc, name='Testing Channel', in_guide=True)
        seed.make_channel_test(ch)
        db.session.commit()
        card = _health_card(self._get(ch, _running_status(ch.id)))
        self.assertNotIn('produced no stream measurements', card)
        self.assertNotIn('during the last test', card)
        self.assertNotIn('No screenshot', card)
        self.assertIn('Testing now. Resolution, bitrate and the rest appear here', card)

    def test_test_now_is_disabled_while_this_channel_tests(self):
        """dev/docs/BUGS.md 2026-10-26: the live refresh redrew the status bar's Test now
        enabled while the test it started was still running. Every Test button, the phone
        bar's included, renders disabled while this channel is the one being tested."""
        ch = seed.make_channel(self.acc, name='Testing Channel', in_guide=True)
        seed.make_channel_test(ch)
        db.session.commit()
        buttons = _test_now_buttons(self._get(ch, _running_status(ch.id)))
        self.assertGreaterEqual(len(buttons), 2)
        for b in buttons:
            self.assertIn(' disabled', b)

    def test_test_now_is_enabled_when_nothing_tests_this_channel(self):
        ch = seed.make_channel(self.acc, name='Idle Channel', in_guide=True)
        seed.make_channel_test(ch)
        db.session.commit()
        for b in _test_now_buttons(self._get(ch)):
            self.assertNotIn(' disabled', b)

    def test_score_from_one_observation_is_dimmed_and_called_early(self):
        """A full-strength green 100 beside "too few to rely on" read as two verdicts."""
        ch = seed.make_channel(self.acc, name='New Channel', health_score=100.0,
                               health_score_sample_count=1)
        db.session.commit()
        body = self._get(ch)
        card = _health_card(body)
        self.assertIn('hs-early', card)
        self.assertIn('Early score', card)
        self.assertNotIn('Lifetime health', card)
        self.assertIn('from 1 observation so far; 2 more tests or recordings', card)
        self.assertRegex(body, r'class="h-mini [^"]*hs-early')

    def test_score_from_three_observations_is_not_early(self):
        ch = seed.make_channel(self.acc, name='Known Channel', health_score=90.0,
                               health_score_sample_count=3)
        db.session.commit()
        body = self._get(ch)
        self.assertNotIn('hs-early', body)
        self.assertIn('Lifetime health', _health_card(body))


class ChannelPageHeadingTests(unittest.TestCase):
    """Both first-time testers read "Channel Search" as the channel page's title."""

    def setUp(self):
        self.t = make_test_app()
        self.ch = seed.make_channel(seed.make_account(), name='Heading Channel')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_the_one_h1_is_the_channel_name_with_the_way_back_above(self):
        body = self.t.client.get(f'/channels/{self.ch.id}').get_data(as_text=True)
        h1s = re.findall(r'<h1[^>]*>(.*?)</h1>', body, re.S)
        self.assertEqual([h.strip() for h in h1s], ['Heading Channel'])
        self.assertIn('class="back" href="/channels">&larr; Channel Search</a>', body)
        self.assertLess(body.index('class="back"'), body.index('<h1'))


if __name__ == '__main__':
    unittest.main()
