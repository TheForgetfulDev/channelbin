"""Tier 0 - the TV Guide's program chooser (dev/changelog/1321, DESIGN.md 12.10 / 13.10).

A click on a program opens the chooser rather than the record dialog: the program in full,
then its actions as icon tiles, Schedule recording first. On a desktop it is the side panel,
on a phone the bottom sheet. Every action hands off to something that already exists, and
these hold each hand-off: the record dialog in each of its modes (new, Record now, edit, the
in-progress controls), Test now and Preview on a group row's serving channel, Play on the
recording player, Copy stream URL, and real links to the recording, the airing search and
the channel or group page. tests/support/program_chooser.mjs drives the real /guide page and
the shipped scripts; every assertion is here.

What it cannot cover: jsdom computes no layout, so nothing here says how the panel or the
tiles look, or whether a phone held sideways scrolls sideways. That is the browser pass.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_program_chooser_js
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'program_chooser.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='program_chooser_js_')
    try:
        with t.app.app_context():
            acc = seed.make_account()
            ch = seed.make_channel(acc, name='Test Channel', in_guide=True)
            db.session.commit()
            page = t.client.get('/guide').get_data(as_text=True)
            meta = {'channelId': ch.id}
        with open(os.path.join(tmp, 'page.html'), 'w', encoding='utf-8') as f:
            f.write(page)
        with open(os.path.join(tmp, 'meta.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f)
        proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
        _RESULT = {'obs': json.loads(proc.stdout), 'meta': meta}
        return _RESULT
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


def _labels(tiles):
    return [t['label'] for t in tiles]


def _tile(tiles, label):
    return next(t for t in tiles if t['label'] == label)


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class _Base:
    SCENARIO = ''

    @classmethod
    def setUpClass(cls):
        res = _observe()
        cls.meta = res['meta']
        cls.obs = res['obs'][cls.SCENARIO]
        if 'error' in cls.obs:
            raise AssertionError(f'{cls.SCENARIO} threw in the page:\n{cls.obs["error"]}')

    def test_the_page_ran_without_errors(self):
        self.assertEqual(self.obs['errors'], [])


class CellClickTests(_Base, unittest.TestCase):
    """The grid's own cell, clicked: the chooser opens, the record dialog does not."""
    SCENARIO = 'cell_click'

    def test_the_grid_drew_a_cell(self):
        self.assertTrue(self.obs['cell'])

    def test_a_click_opens_the_side_panel_not_the_record_dialog(self):
        o = self.obs['opened']
        self.assertTrue(o['panelOpen'])
        self.assertFalse(o['recordModal']['open'])
        self.assertEqual(o['title'], 'Evening News')

    def test_the_program_is_shown_in_full_with_the_title_once(self):
        head = self.obs['opened']['progHead']
        self.assertIn('Tonight', head['text'])
        self.assertIn('The day in full.', head['text'])
        self.assertFalse(head['hasTitleHeading'], 'the panel heading already carries the title')

    def test_a_reread_of_the_grid_refreshes_the_open_panel(self):
        after = self.obs['afterRefresh']
        self.assertTrue(after['panelOpen'])
        self.assertEqual(after['tiles'][0]['label'], 'Edit recording')


class UpcomingTests(_Base, unittest.TestCase):
    SCENARIO = 'upcoming'

    def test_actions_in_order_schedule_recording_first(self):
        self.assertEqual(_labels(self.obs['opened']['tiles']),
                         ['Schedule recording', 'Test now', 'Preview', 'Copy stream URL',
                          'Other airings', 'Channel page'])

    def test_record_is_drawn_with_the_registry_record_icon(self):
        self.assertEqual(self.obs['opened']['tiles'][0]['icon'], 'record')

    def test_navigation_tiles_are_real_links(self):
        tiles = self.obs['opened']['tiles']
        ch = _tile(tiles, 'Channel page')
        self.assertEqual((ch['tag'], ch['href']), ('a', f'/channels/{self.meta["channelId"]}'))
        air = _tile(tiles, 'Other airings')
        self.assertEqual(air['tag'], 'a')
        self.assertIn('in=epg-title', air['href'])
        self.assertIn('grain=airings', air['href'])
        self.assertIn('q=Evening%20News', air['href'])

    def test_no_recording_page_without_a_recording(self):
        self.assertNotIn('Recording page', _labels(self.obs['opened']['tiles']))

    def test_schedule_recording_replaces_the_chooser_with_the_record_dialog(self):
        after = self.obs['afterRecord']
        self.assertFalse(after['panelOpen'])
        self.assertEqual(after['recordModal'], {'open': True, 'title': 'Schedule recording'})


class ScheduledTests(_Base, unittest.TestCase):
    SCENARIO = 'scheduled'

    def test_offers_edit_and_the_recording_page(self):
        tiles = self.obs['tiles']
        self.assertEqual(tiles[0]['label'], 'Edit recording')
        self.assertEqual(_tile(tiles, 'Recording page')['href'], '/recordings/77')
        self.assertNotIn('Play', _labels(tiles))

    def test_edit_opens_the_record_dialog_in_edit_mode(self):
        self.assertEqual(self.obs['afterEdit'],
                         {'panelOpen': False, 'recordModal': {'open': True, 'title': 'Edit recording'}})


class CapturingTests(_Base, unittest.TestCase):
    SCENARIO = 'capturing'

    def test_offers_controls_play_and_the_recording_page(self):
        labels = _labels(self.obs['tiles'])
        self.assertEqual(labels[:2], ['Recording controls', 'Play'])
        self.assertIn('Recording page', labels)

    def test_controls_open_the_in_progress_dialog(self):
        self.assertEqual(self.obs['controls'],
                         {'panelOpen': False, 'activeOpen': True, 'recordModalOpen': False})

    def test_play_opens_the_recording_player_on_this_recording(self):
        after = self.obs['afterPlay']
        self.assertFalse(after['panelOpen'])
        self.assertEqual(after['dialogs'], ['Play - Evening News'])
        self.assertEqual(after['gets'], ['/recordings/78/file/check'])


class EndedTests(_Base, unittest.TestCase):
    SCENARIO = 'ended'

    def test_schedule_recording_is_greyed_and_says_why(self):
        self.assertTrue(self.obs['tiles'][0]['disabled'])
        self.assertEqual(self.obs['note'],
                         'This program has ended. Try Other airings for another showing.')

    def test_the_greyed_tile_does_nothing(self):
        self.assertTrue(self.obs['afterClick']['panelOpen'])
        self.assertFalse(self.obs['afterClick']['recordModal']['open'])

    def test_test_and_preview_stay_on_offer(self):
        self.assertIn('Test now', _labels(self.obs['tiles']))
        self.assertIn('Preview', _labels(self.obs['tiles']))


class DummyTests(_Base, unittest.TestCase):
    """A filler slot on a channel with no listings: no program to search for, and still a
    When line rather than an empty box."""
    SCENARIO = 'dummy'

    def test_no_other_airings_and_the_note_does_not_point_at_it(self):
        self.assertNotIn('Other airings', _labels(self.obs['tiles']))
        self.assertEqual(self.obs['note'], 'This program has ended, so it cannot be recorded.')

    def test_shows_when_and_channel(self):
        self.assertIn('When', self.obs['progHead']['text'])
        self.assertIn('Test Channel', self.obs['progHead']['text'])


class GroupRowTests(_Base, unittest.TestCase):
    """A group row tests and previews the channel it records from now, and says so."""
    SCENARIO = 'group'

    def test_links_to_the_group_page(self):
        tile = _tile(self.obs['tiles'], 'Channel group page')
        self.assertEqual(tile['href'], '/channel-groups/5')
        self.assertNotIn('Channel page', _labels(self.obs['tiles']))

    def test_names_the_serving_channel(self):
        self.assertIn('US: ESPN FHD', self.obs['servingNote'])

    def test_test_now_runs_on_the_serving_channel_and_keeps_the_panel(self):
        after = self.obs['afterTest']
        self.assertEqual(after['posts'], ['/api/channels/12/test-now'])
        self.assertTrue(after['panelOpen'])
        self.assertIn('US: ESPN FHD', after['toast'])

    def test_copy_copies_the_url_and_the_toast_never_shows_it(self):
        after = self.obs['afterCopy']
        self.assertEqual(after['copied'], ['http://example.test/live/u1/p1/9.ts'])
        self.assertNotIn('example.test', after['toast'])
        self.assertIn('login', after['toast'])

    def test_preview_plays_the_serving_channel_and_replaces_the_chooser(self):
        after = self.obs['afterPreview']
        self.assertIn('/api/channels/12/preview', after['posts'])
        self.assertFalse(after['panelOpen'])
        self.assertEqual(after['dialogs'], ['Preview - US: ESPN FHD'])


class RefillAndEscapeTests(_Base, unittest.TestCase):
    SCENARIO = 'refill_and_escape'

    def test_a_second_program_refills_the_one_panel(self):
        self.assertEqual(self.obs['refilled'], {'count': 1, 'title': 'Late Show'})

    def test_escape_closes_the_dialog_on_top_first_then_the_panel(self):
        self.assertEqual(self.obs['afterFirst'], {'panelOpen': True, 'dialogs': []})
        self.assertFalse(self.obs['afterSecond']['panelOpen'])


class SingleChannelTests(_Base, unittest.TestCase):
    """The channel page's What's on card is already that channel's page."""
    SCENARIO = 'single_channel'

    def test_no_link_to_the_page_it_is_on(self):
        self.assertNotIn('Channel page', _labels(self.obs['tiles']))


class PhoneTests(_Base, unittest.TestCase):
    SCENARIO = 'phone'

    def test_a_phone_gets_the_sheet_with_the_program_at_its_top(self):
        o = self.obs['opened']
        self.assertFalse(o['panelOpen'])
        self.assertEqual(o['dialogs'], ['Evening News'])
        self.assertEqual(o['tiles'][0]['label'], 'Record now')
        self.assertIn('The day in full.', o['progHead']['text'])

    def test_record_now_closes_the_sheet_and_opens_the_record_dialog(self):
        after = self.obs['afterRecord']
        self.assertEqual(after['recordModal'], {'open': True, 'title': 'Record now'})
        self.assertNotIn('Evening News', after['dialogs'])


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


class WiringTests(unittest.TestCase):
    """Both pages that draw a guide grid open the chooser and load what its actions call."""

    def test_both_cell_click_sites_open_the_chooser(self):
        js = _read('static/js/guide.js')
        self.assertEqual(js.count("addEventListener('click', () => openProgramChooser(prog, ch))"), 2)
        self.assertNotIn("addEventListener('click', () => openModal(prog, ch))", js)

    def test_each_grid_page_loads_the_chooser_after_guide_js(self):
        for rel in ('templates/guide.html', 'templates/channels/detail.html'):
            html = _read(rel)
            g = html.index("filename='js/guide.js'")
            for script in ('program-chooser.js', 'recording-player.js', 'live-preview.js'):
                self.assertIn(f"filename='js/{script}'", html, f'{rel} lacks {script}')
            self.assertGreater(html.index("filename='js/program-chooser.js'"), g, rel)
            for script in ('recording-player.js', 'live-preview.js'):
                self.assertRegex(html, rf"js/{re.escape(script)}'\) }}}}\"\s+data-hls-js=\"[^\"]*hls\.light\.min\.js",
                                 f'{rel}: {script} needs its data-hls-js')
            self.assertIn('programAiringsUrl:', html, rel)


class AiringsUrlRouteTests(unittest.TestCase):
    """The airings link is built by the server's one speller, narrowed to program titles."""

    def test_the_guide_page_renders_a_title_only_airings_url(self):
        t = make_test_app()
        try:
            with t.app.app_context():
                acc = seed.make_account()
                seed.make_channel(acc, name='Test Channel', in_guide=True)
                db.session.commit()
                html = t.client.get('/guide').get_data(as_text=True)
            m = re.search(r'programAiringsUrl: "([^"]*)"', html)
            self.assertIsNotNone(m)
            self.assertIn('in=epg-title', m.group(1))
            self.assertIn('grain=airings', m.group(1))
        finally:
            t.cleanup()


if __name__ == '__main__':
    unittest.main()
