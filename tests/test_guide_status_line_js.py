"""Tier 0 - the guide's count line does not call its own placeholder slots programs
(BUGS.md 2026-11-04 @ 03:20:10 PM, dev/changelog/1436).

A channel with no listings in the window gets one-hour slots titled with its own name, built
per request and never stored. The channel page's What's On said "73 of 73 programs" over those
and the TV Guide added them to its total, while search correctly found nothing. Runs the shipped
util.js + guide.js against the markup and the /api/guide/epg payload the real routes produced;
tests/support/guide_status_line.mjs drives it and reports.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import db  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'guide_status_line.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

NO_LISTINGS = 'Slotless Sports 05'
LISTED = 'Listed Network'

_RESULT = None


def _epg(client, channel_id=None):
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    url = (f'/api/guide/epg?start={now - timedelta(hours=1):%Y-%m-%dT%H:%M:%S}'
           f'&end={now + timedelta(hours=24):%Y-%m-%dT%H:%M:%S}')
    if channel_id is not None:
        url += f'&channel_id={channel_id}'
    return client.get(url).get_json()


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='guide_status_line_js_')
    try:
        with t.app.app_context():
            acc = seed.make_account()
            bare = seed.make_channel(acc, name=NO_LISTINGS, in_guide=True)
            # What the channel page needs to draw the card at all: a stored listing, here one
            # long ended, which is the shape of a game channel between its placeholder guides.
            seed.make_epg_entry(bare, title='Yesterday', offset_minutes=-3 * 24 * 60)
            listed = seed.make_channel(acc, name=LISTED, in_guide=True)
            # A listing that only repeats the channel's name is still the provider's listing.
            seed.make_epg_entry(listed, title=LISTED, offset_minutes=30)
            seed.make_epg_entry(listed, title='Late Show', offset_minutes=120)
            db.session.commit()
            pages = {
                'guide': t.client.get('/guide').get_data(as_text=True),
                'channel': t.client.get(f'/channels/{bare.id}').get_data(as_text=True),
            }
            bare_epg = _epg(t.client, bare.id)
            listed_epg = _epg(t.client, listed.id)
            guide_epg = _epg(t.client)
        for name, html in pages.items():
            with open(os.path.join(tmp, f'{name}.html'), 'w', encoding='utf-8') as f:
                f.write(html)
        cases = {
            'channel_placeholders': {'page': 'channel', 'epg': bare_epg},
            'channel_placeholders_text_hits_name': {'page': 'channel', 'epg': bare_epg, 'text': 'slotless'},
            'channel_placeholders_text_misses': {'page': 'channel', 'epg': bare_epg, 'text': 'nothing-like-this'},
            'channel_listings': {'page': 'channel', 'epg': listed_epg},
            'channel_listings_filtered': {'page': 'channel', 'epg': listed_epg, 'text': 'late'},
            'guide_mixed': {'page': 'guide', 'epg': guide_epg},
            'guide_only_listed': {'page': 'guide', 'epg': listed_epg},
            'guide_only_placeholders': {'page': 'guide', 'epg': bare_epg},
            'guide_filter_hides_slots': {'page': 'guide', 'epg': bare_epg, 'dur_max': 30},
        }
        with open(os.path.join(tmp, 'cases.json'), 'w', encoding='utf-8') as f:
            json.dump(cases, f)
        proc = subprocess.run(['node', HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
        return _RESULT
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class GuideStatusLineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()

    def test_every_case_booted_clean(self):
        for name, case in self.obs.items():
            self.assertEqual(case['errors'], [], name)

    def test_channel_page_with_only_placeholders_says_so_and_claims_no_programs(self):
        case = self.obs['channel_placeholders']
        self.assertGreater(case['slots_drawn'], 0, 'the slots themselves stay')
        self.assertEqual(case['line'], 'No listings for this time, showing placeholder slots')

    def test_a_filter_still_shows_beside_the_placeholder_message(self):
        self.assertEqual(self.obs['channel_placeholders_text_hits_name']['line'],
                         'No listings for this time, showing placeholder slots · text "slotless" · gaps collapsed')

    def test_a_filter_that_hides_the_slots_does_not_claim_they_are_showing(self):
        case = self.obs['channel_placeholders_text_misses']
        self.assertTrue(case['empty_state'])
        self.assertEqual(case['line'], 'No listings for this time · text "nothing-like-this" · gaps collapsed')

    def test_provider_listings_keep_counting_even_when_they_repeat_the_channel_name(self):
        self.assertEqual(self.obs['channel_listings']['slots_drawn'], 0)
        self.assertEqual(self.obs['channel_listings']['line'], '2 of 2 programs · no filter')

    def test_a_filter_on_real_listings_reads_as_before(self):
        self.assertEqual(self.obs['channel_listings_filtered']['line'],
                         '1 of 2 programs · text "late" · gaps collapsed')

    def test_guide_total_counts_listings_and_names_the_rows_on_placeholders(self):
        self.assertEqual(self.obs['guide_mixed']['line'],
                         '2 channels · 2 programs over 3 days · 1 with no listings')

    def test_guide_says_nothing_extra_when_every_row_has_listings(self):
        self.assertEqual(self.obs['guide_only_listed']['line'],
                         '1 channel · 2 programs over 3 days')

    def test_guide_of_only_placeholders_counts_no_programs(self):
        case = self.obs['guide_only_placeholders']
        self.assertGreater(case['slots_drawn'], 0)
        self.assertEqual(case['line'], '1 channel · 0 programs over 3 days · 1 with no listings')
        self.assertFalse(case['empty_state'], 'slots on screen are not the empty state')

    def test_the_empty_state_still_goes_by_what_the_filter_left_on_screen(self):
        self.assertTrue(self.obs['guide_filter_hides_slots']['empty_state'])


if __name__ == '__main__':
    unittest.main()
