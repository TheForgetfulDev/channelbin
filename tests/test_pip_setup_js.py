"""The picture-in-picture card and its setup dialog (static/js/pip-setup.js, iptv-output.js,
util.js::wireModal's backdropHandler), driven in jsdom over the page and the pickers the app
rendered (tests/support/pip_setup_dialog.mjs), per mockup 63 and
dev/docs/DESIGN-pip-channels.md 12. Throwaway temp SQLite DB - never the live dvr.db. No
dev/docs/BUGS.md entry: a feature.
  python3 -m unittest tests.test_pip_setup_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.output_feed import FeedCase  # noqa: E402
from tests.support.seed import make_guide_match  # noqa: E402

import app.pip_channels as pip  # noqa: E402
from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (PIP_SOURCE_CHANNEL, PIP_SOURCE_GROUP, SportsEvent, SportsFollow,  # noqa: E402
                          SportsLeague)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'pip_setup_dialog.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
_OBSERVED = {}


@unittest.skipIf(shutil.which('node') is None or not os.path.isdir(JSDOM), 'node or jsdom not installed')
class PipSetupDialogTests(FeedCase):
    def setUp(self):
        # One render and one harness run for the whole class; the rig builds its app per test.
        if _OBSERVED:
            self.obs = _OBSERVED
            return
        super().setUp()
        league = SportsLeague(tsdb_id=9001, name='Pretend League', match_terms='[]')
        db.session.add(league)
        db.session.flush()
        db.session.add(SportsFollow(league_id=league.id, team_tsdb_id=0, rule_id=0))
        now = datetime.utcnow()
        for n, (ahead, home) in enumerate(((timedelta(minutes=-10), 'Ironvale Bears'),
                                           (timedelta(days=3), 'Saltmarsh Owls'))):
            event = SportsEvent(tsdb_id=7001 + n, league_id=league.id, name='g', home_team_id=101 + n,
                                away_team_id=201 + n, home_name=home, away_name='Harborview Gulls',
                                start_at=now + ahead, local_date=(now + ahead).date())
            db.session.add(event)
            db.session.flush()
            make_guide_match(event, self.beta, now + ahead, title=f'g{n}', reason='r')
        db.session.commit()
        cfg = load_config()
        for name, sources in (('PiP 1', {'main': {'kind': PIP_SOURCE_CHANNEL, 'ref': self.alpha.id},
                                         'inset': {'kind': PIP_SOURCE_GROUP, 'ref': self.group.id}}),
                              ('PiP 2', {'main': {'kind': PIP_SOURCE_CHANNEL, 'ref': self.beta.id},
                                         'inset': {'kind': PIP_SOURCE_CHANNEL, 'ref': self.alpha.id}})):
            row = pip.create(pip.clean({'name': name, **sources}))
        pip.clear(row.id)
        html = self.client.get('/iptv-output').get_data(as_text=True)
        pickers = self.client.get('/api/iptv-output/pip/pickers').get_json()
        self.assertTrue(cfg and pickers['games'])
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'page.html'), 'w', encoding='utf-8') as fh:
                fh.write(html)
            with open(os.path.join(tmp, 'pickers.json'), 'w', encoding='utf-8') as fh:
                json.dump(pickers, fh)
            proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO], capture_output=True,
                                  text=True, cwd=REPO, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr[-4000:]}')
        _OBSERVED.update(json.loads(proc.stdout), alpha=self.alpha.id, beta=self.beta.id, group=self.group.id)
        self.obs = _OBSERVED

    def test_no_script_errors(self):
        self.assertEqual(self.obs['errors'], [])

    def test_create_opens_empty_and_says_why_it_cannot_create_yet(self):
        o = self.obs
        self.assertEqual((o['create']['open'], o['create']['title'], o['create']['picking']),
                         (True, 'Create a PiP channel', False))
        self.assertEqual(o['create_note'], 'Choose what the big picture shows.')
        self.assertEqual(o['create_disabled'], 'true')
        self.assertEqual(o['create_refused_posts'], [], 'nothing is created until it can be')

    def test_a_picker_takes_the_dialogs_place_with_a_back_arrow_for_the_x(self):
        p = self.obs['picker']
        self.assertEqual((p['picking'], p['close_hidden'], p['back_hidden'], p['foot_hidden']),
                         (True, True, False, True))
        self.assertEqual(p['title'], 'Big picture: choose what it shows')
        self.assertEqual(self.obs['picker_tabs'], ['A game', 'A channel or group', 'A search'])
        self.assertEqual(self.obs['picker_days'][0], 'On now')
        self.assertEqual(len(self.obs['picker_days']), 2)

    def test_escape_the_backdrop_and_back_each_step_back_and_keep_the_dialog(self):
        for key in ('after_escape', 'after_backdrop', 'after_back'):
            v = self.obs[key]
            self.assertEqual((v['open'], v['picking'], v['close_hidden'], v['back_hidden'], v['foot_hidden']),
                             (True, False, False, True, False), key)
            self.assertEqual(v['title'], 'Create a PiP channel')

    def test_on_the_main_view_the_backdrop_closes_the_dialog(self):
        self.assertTrue(self.obs['backdrop_closes_main_view'])

    def test_a_choice_returns_to_the_main_view_and_names_the_picture(self):
        self.assertFalse(self.obs['after_pick']['picking'])
        self.assertIn('Pretend League: Harborview Gulls at Ironvale Bears', self.obs['main_row'])
        self.assertIn('On now', self.obs['main_row'])
        self.assertEqual(self.obs['standin_big'], 'Harborview Gulls at Ironvale Bears')

    def test_the_game_the_other_picture_has_cannot_be_chosen_twice(self):
        self.assertTrue(self.obs['taken_disabled'])
        self.assertTrue(self.obs['taken_says'])

    def test_channels_and_groups_are_listed_in_guide_order_and_filtered(self):
        self.assertEqual(self.obs['channel_rows'], ['Alpha', 'Game feeds', 'Beta'])
        self.assertEqual(self.obs['channel_filtered'], ['Beta'])

    def test_the_stand_in_follows_the_layout_from_the_servers_boxes(self):
        o = self.obs
        self.assertEqual(len({o['box_corner'], o['box_top_left'], o['box_side']}), 3)
        self.assertIn('left:2.50%', o['box_top_left'])
        self.assertIn('left:50.00%', o['box_side'])
        self.assertEqual(o['corner_disabled_side_by_side'], 'true')

    def test_create_posts_what_was_chosen_once(self):
        o = self.obs
        self.assertEqual(o['note_without_name'], 'Give it a name.')
        self.assertEqual(o['note_ready'], '')
        self.assertEqual(o['create_posts'][0], 'POST /api/iptv-output/pip')
        self.assertEqual(sum(1 for p in o['create_posts'] if p.startswith('POST')), 1)
        self.assertEqual(o['create_body'], {
            'name': 'Race day', 'main': {'kind': 'game', 'ref': 7001, 'query': ''},
            'inset': {'kind': 'channel', 'ref': o['beta'], 'query': ''},
            'layout': 'side', 'corner': 'tl', 'size': 'm', 'swapped_copy': False})
        self.assertTrue(o['closed_after_create'])
        self.assertIn('is on the output as channel 7', o['create_toast'])

    def test_change_opens_on_the_stored_setup(self):
        self.assertEqual(self.obs['change']['title'], 'Change PiP 1')
        self.assertEqual(self.obs['change_name'], 'PiP 1')
        self.assertEqual(self.obs['change_rows'], ['Alpha', 'Game feeds'])

    def test_each_picture_says_what_it_would_play_from_and_the_dialog_counts_connections(self):
        o = self.obs
        self.assertEqual(o['would_main'], 'Would play from Provider one',
                         'a picture that is one channel already names it')
        self.assertEqual(o['would_inset'], 'Would play from Beta on Provider two',
                         'a group names the channel it would start from')
        self.assertIn('Right now 2 of your 3 connections are free.', o['would_seats'])
        self.assertFalse(o['would_seats_hidden'])
        self.assertEqual(o['would_body'], {'main': {'kind': 'channel', 'ref': o['alpha'], 'query': ''},
                                           'inset': {'kind': 'group', 'ref': o['group'], 'query': ''}})
        self.assertEqual(o['would_asks_for_a_name'], 0, 'asked when a picture changes, not at every redraw')

    def test_a_search_is_used_only_with_words_and_lists_what_matches(self):
        o = self.obs
        self.assertTrue(o['use_search_off'])
        self.assertEqual(o['search_hits'], ['Alpha'])
        self.assertIn('Channels named "alp"', o['search_row'])
        self.assertEqual(o['save_posts'][0], 'POST /api/iptv-output/pip/1')
        self.assertEqual(o['save_body']['inset'], {'kind': 'search', 'ref': None, 'query': 'alp'})
        self.assertIn('keeps the picture it has until it tunes in again', o['save_toast'])

    def test_the_rows_switch_and_remove(self):
        o = self.obs
        self.assertEqual(o['twin_posts'][0], 'POST /api/iptv-output/pip/1/swapped-copy')
        self.assertEqual(o['twin_body'], {'on': True})
        self.assertIn('leaves the channel list IPTV players get', o['remove_asks'])
        self.assertEqual(o['remove_cancel_posts'], [])
        self.assertEqual(o['remove_posts'][0], 'DELETE /api/iptv-output/pip/1')
        self.assertTrue(o['empty_swap_disabled'])
        self.assertTrue(o['empty_twin_disabled'])


if __name__ == '__main__':
    unittest.main()
