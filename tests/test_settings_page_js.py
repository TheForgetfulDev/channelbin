"""Tier 0 - the Settings page's Basic/Advanced view, search and changed filter in a real DOM.

The view, the query and the changed-from-default chip are three inputs to one filter
function in static/js/settings.js (DESIGN.md 15.9, dev/changelog/1005 and 1006), which also
dims a row whose gate is off from the gating controls' live values (1007). The server
renders all 115 rows either way, so what Basic hides, what a filter in Basic brings back,
how it says so, and what gets saved are all browser behavior - none of it is visible from a
response body.

tests/support/settings_page.mjs runs the shipped util.js and settings.js against the page
the Flask app really answered with the view pref unset and with it saved as Advanced, and
reports; every assertion lives here.

What it cannot cover: jsdom computes no layout, so where the switch sits at 375px, and
whether switching view keeps the section being read in place, are browser work.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_settings_page_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app.routes.settings import SETTINGS_VIEW_PREF  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'settings_page.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

# The cards with no Basic field (DESIGN.md 15.9). System is not here: its LAN-exposure
# warning has no tier and shows whenever the bind address is open.
EMPTY_IN_BASIC = {'watchdog': 11, 'http': 1, 'logging': 2, 'backups': 4, 'debug': 2}

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='settings_page_js_')
    try:
        t.app.config['WTF_CSRF_ENABLED'] = False
        client = t.app.test_client()

        def snapshot(name):
            resp = client.get('/settings')
            if resp.status_code != 200:
                raise AssertionError(f'/settings answered {resp.status_code} for {name}')
            with open(os.path.join(tmp, f'{name}.html'), 'w', encoding='utf-8') as f:
                f.write(resp.get_data(as_text=True))

        snapshot('plain')
        # One Basic field (Recording) and two Advanced ones (Watchdog, Sync), saved the way
        # the page saves them.
        for path, value in (('recording.retention_days', 30),
                            ('watchdog.poll_interval_seconds', 9),
                            ('sync.epg_collapse_threshold_percent', 35)):
            r = client.post('/api/settings/field', json={'path': path, 'value': value})
            if not (r.get_json() or {}).get('changed_from_default'):
                raise AssertionError(f'saving {path} did not report a change: {r.get_data(as_text=True)}')
        snapshot('basic')
        client.post(f'/api/user-prefs/{SETTINGS_VIEW_PREF}', json={'value': True})
        snapshot('advanced')

        proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
        return _RESULT
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class _Base(unittest.TestCase):
    SCENARIO = ''

    @classmethod
    def setUpClass(cls):
        if not cls.SCENARIO:
            raise unittest.SkipTest('base class - carries the shared cases only')
        cls.obs = _observe()[cls.SCENARIO]
        if 'error' in cls.obs:
            raise AssertionError(f'{cls.SCENARIO} threw in the page:\n{cls.obs["error"]}')

    def test_the_page_ran_without_errors(self):
        self.assertEqual(self.obs['errors'], [])


class NeverChosenOpensInBasicTests(_Base):
    SCENARIO = 'basic_boot'

    def test_only_the_basic_fields_show(self):
        self.assertEqual(self.obs['view'], 'basic')
        self.assertEqual(len(self.obs['shownPaths']), 23)
        self.assertEqual(self.obs['checked'], ['basic'])
        self.assertEqual(self.obs['on'], ['basic'])

    def test_the_script_takes_over_from_the_first_paint_rule(self):
        self.assertTrue(self.obs['ready'])

    def test_opening_the_page_saves_nothing(self):
        self.assertEqual(self.obs['posts'], [])

    def test_a_card_with_nothing_basic_collapses_with_its_count_and_stays_on_the_rail(self):
        for sec, n in EMPTY_IN_BASIC.items():
            with self.subTest(sec=sec):
                s = self.obs['sections'][sec]
                self.assertTrue(s['collapsed'])
                self.assertEqual(s['head'], f"{n} advanced setting{'' if n == 1 else 's'}")
                self.assertTrue(s['railDim'])
                self.assertEqual(s['rail'], '0')

    def test_a_split_card_says_how_many_more_are_in_advanced(self):
        rec = self.obs['sections']['recording']
        self.assertFalse(rec['collapsed'])
        self.assertEqual(rec['rail'], '12')
        self.assertEqual(rec['more'], '16 more in Advanced Show Advanced')
        self.assertIsNone(self.obs['sections']['integrations']['more'])

    def test_a_group_with_nothing_basic_hides_its_heading(self):
        self.assertEqual(self.obs['sections']['recording']['hiddenGroups'],
                         ['conversion-resilience', 'collision-avoidance', 'measurement'])

    def test_the_basic_forms_stay(self):
        self.assertEqual(self.obs['sections']['security']['hiddenUnits'], 0)
        self.assertEqual(self.obs['sections']['integrations']['hiddenUnits'], 0)

    def test_the_search_box_counts_every_setting_because_search_reaches_all_of_them(self):
        self.assertEqual(self.obs['placeholder'], 'Search 115 settings by name, description or key')


class SearchInBasicTests(_Base):
    SCENARIO = 'basic_search'

    def test_a_match_outside_basic_is_shown_and_marked(self):
        during = self.obs['during']
        self.assertIn('watchdog.stall_timeout_seconds', during['shownAdvancedHits'])
        self.assertEqual(len(during['shownAdvancedHits']), 11)
        self.assertFalse(during['sections']['watchdog']['collapsed'])

    def test_the_page_says_the_matches_are_advanced_and_offers_the_switch(self):
        during = self.obs['during']
        self.assertEqual(
            during['notice'],
            '11 matches are Advanced settings. They are marked Advanced and show only while '
            'you search. Show Advanced')
        self.assertEqual(during['noticeAction'], 'Show Advanced')

    def test_counts_are_matches_and_spelled_right(self):
        during = self.obs['during']
        self.assertEqual(during['chip'], '11 of 115 settings')
        self.assertEqual(during['sections']['watchdog']['head'], '5 matches')
        self.assertIsNone(during['sections']['recording']['more'])

    def test_clearing_the_search_goes_back_to_basic_and_saves_nothing(self):
        cleared = self.obs['cleared']
        self.assertEqual(len(cleared['shownPaths']), 23)
        self.assertIsNone(cleared['notice'])
        self.assertEqual(self.obs['posts'], [])


class NoticeSwitchTests(_Base):
    SCENARIO = 'notice_switch'

    def test_show_advanced_saves_the_choice_and_keeps_what_was_found(self):
        self.assertEqual(self.obs['after']['view'], 'advanced')
        self.assertIsNone(self.obs['after']['notice'])
        self.assertEqual(self.obs['posts'],
                         [{'path': f'/api/user-prefs/{SETTINGS_VIEW_PREF}', 'body': {'value': True}}])
        self.assertEqual(len(self.obs['cleared']['shownPaths']), 115)


class FooterSwitchTests(_Base):
    SCENARIO = 'footer_switch'

    def test_a_card_footer_switches_the_whole_page_and_saves_it(self):
        self.assertEqual(self.obs['view'], 'advanced')
        self.assertEqual(len(self.obs['shownPaths']), 115)
        self.assertEqual(self.obs['posts'],
                         [{'path': f'/api/user-prefs/{SETTINGS_VIEW_PREF}', 'body': {'value': True}}])


class SavedAdvancedTests(_Base):
    SCENARIO = 'advanced_boot'

    def test_a_saved_advanced_choice_opens_in_advanced(self):
        opened = self.obs['opened']
        self.assertEqual(opened['view'], 'advanced')
        self.assertEqual(opened['checked'], ['advanced'])
        self.assertEqual(len(opened['shownPaths']), 115)
        self.assertFalse(any(s['collapsed'] for s in opened['sections'].values()))
        self.assertFalse(any(s['more'] for s in opened['sections'].values()))

    def test_switching_back_saves_basic_once(self):
        self.assertEqual(len(self.obs['switched']['shownPaths']), 23)
        self.assertEqual(self.obs['posts'],
                         [{'path': f'/api/user-prefs/{SETTINGS_VIEW_PREF}', 'body': {'value': False}}])


class DeepLinkTests(_Base):
    SCENARIO = 'deep_link'

    def test_a_link_to_an_advanced_setting_lands_on_it_in_basic(self):
        self.assertEqual(self.obs['shownPaths'], ['watchdog.early_fail_abort_count'])
        self.assertEqual(self.obs['view'], 'basic')
        self.assertTrue(self.obs['notice'].startswith('1 match is an Advanced setting.'))


CHANGED = ['recording.retention_days', 'watchdog.poll_interval_seconds',
           'sync.epg_collapse_threshold_percent']


class ChangedChipTests(_Base):
    SCENARIO = 'changed_chip'

    def test_the_marks_come_from_the_server_and_the_chip_counts_them(self):
        before = self.obs['before']
        self.assertEqual(before['changedPaths'], CHANGED)
        self.assertEqual(before['changedChip'],
                         {'active': False, 'pressed': 'false', 'disabled': False, 'count': '3'})

    def test_the_chip_shows_only_changed_rows_across_tiers(self):
        on = self.obs['on']
        self.assertEqual(on['shownPaths'], CHANGED)
        self.assertEqual(on['shownAdvancedHits'], CHANGED[1:])
        self.assertEqual(on['changedChip']['active'], True)
        self.assertEqual(on['changedChip']['pressed'], 'true')
        self.assertEqual(on['view'], 'basic')

    def test_the_counts_say_changed(self):
        on = self.obs['on']
        self.assertEqual(on['chip'], '3 of 115 settings')
        self.assertEqual(on['sections']['recording']['head'], '1 changed')
        self.assertEqual(on['sections']['watchdog']['head'], '1 changed')
        self.assertFalse(on['sections']['watchdog']['collapsed'])
        self.assertEqual(on['sections']['display']['head'], 'none changed')
        self.assertTrue(on['sections']['display']['collapsed'])
        self.assertEqual(on['sections']['watchdog']['rail'], '1')
        self.assertIsNone(on['sections']['recording']['more'])

    def test_the_notice_names_the_filter_not_the_search(self):
        self.assertEqual(
            self.obs['on']['notice'],
            '2 changed settings are Advanced. They are marked Advanced and show only while '
            'Changed from default is on. Show Advanced')

    def test_the_forms_hide_while_it_is_on(self):
        self.assertEqual(self.obs['on']['sections']['security']['hiddenUnits'], 1)
        self.assertEqual(self.obs['on']['sections']['integrations']['hiddenUnits'], 1)

    def test_the_address_bar_carries_it_for_a_reload(self):
        self.assertEqual(self.obs['on']['url'], '?changed=1')
        self.assertEqual(self.obs['off']['url'], '')

    def test_a_search_narrows_within_it(self):
        self.assertEqual(self.obs['withSearch']['shownPaths'], ['watchdog.poll_interval_seconds'])
        self.assertEqual(self.obs['withSearch']['sections']['watchdog']['head'], '1 match')

    def test_turning_it_off_goes_back_to_basic_and_saves_nothing(self):
        off = self.obs['off']
        self.assertEqual(len(off['shownPaths']), 23)
        self.assertIsNone(off['notice'])
        self.assertEqual(off['changedChip']['active'], False)
        self.assertEqual(self.obs['posts'], [])


class ChangedSaveTests(_Base):
    SCENARIO = 'changed_save'

    def test_a_save_back_to_the_default_clears_the_mark_and_the_row_leaves_the_list(self):
        self.assertEqual(self.obs['posts'], [{'path': '/api/settings/field',
                                              'body': {'path': 'recording.retention_days', 'value': 0}}])
        self.assertEqual(self.obs['changedPaths'], CHANGED[1:])
        self.assertEqual(self.obs['shownPaths'], CHANGED[1:])
        self.assertEqual(self.obs['changedChip']['count'], '2')


class ChangedReloadTests(_Base):
    SCENARIO = 'changed_reload'

    def test_a_reload_with_changed_1_lands_on_the_same_list(self):
        self.assertEqual(self.obs['shownPaths'], CHANGED)
        self.assertTrue(self.obs['changedChip']['active'])


class NothingChangedTests(_Base):
    SCENARIO = 'changed_none'

    def test_the_chip_stays_and_says_zero(self):
        self.assertEqual(self.obs['opened']['changedPaths'], [])
        self.assertEqual(self.obs['opened']['changedChip'],
                         {'active': False, 'pressed': 'false', 'disabled': True, 'count': '0'})

    def test_a_disabled_chip_does_nothing(self):
        self.assertEqual(len(self.obs['clicked']['shownPaths']), 23)
        self.assertIsNone(self.obs['clicked']['verdict'] or None)


class NothingChangedReloadTests(_Base):
    SCENARIO = 'changed_none_reload'

    def test_the_page_says_nothing_is_changed_and_the_chip_can_still_turn_off(self):
        opened = self.obs['opened']
        self.assertEqual(opened['verdict'],
                         'No setting on this page is changed from its default. Show all settings')
        self.assertEqual(opened['changedChip']['disabled'], False)
        self.assertEqual(len(self.obs['cleared']['shownPaths']), 23)
        self.assertIsNone(self.obs['cleared']['verdict'] or None)


PP = 'recording.post_process'
PP_OFF = 'Not used while Post-process (remux) is off'


class GatedRowTests(_Base):
    """A field nothing reads while a switch or mode elsewhere on the page says so fades and
    shows a badge naming that gate, and follows the control live (DESIGN.md 15.9, dev/changelog/1007)."""
    SCENARIO = 'gates'

    def test_the_first_paint_follows_the_values_on_the_page(self):
        # _DEFAULTS: move on complete, the post-script and the login gate are off; post-process
        # is on, mp4, re-encode when damaged, auto-restart on, collisions paused.
        self.assertEqual(self.obs['opened']['gated'], {
            'recording.move_on_complete.destination': 'Not used while Move on complete is off',
            'recording.post_script.path': 'Not used while Run post-completion script is off',
            'auth.session_timeout_minutes': 'Not used while Require a password is off',
        })
        self.assertTrue(self.obs['opened']['emptyLinesWhenUngated'])

    def test_turning_post_process_off_dims_all_twelve_of_its_rows_before_the_save_answers(self):
        gated = self.obs['ppOff']['gated']
        pp_rows = {k: v for k, v in gated.items() if k.startswith(PP)}
        self.assertEqual(len(pp_rows), 12)
        self.assertEqual(set(pp_rows.values()), {PP_OFF})

    def test_turning_it_back_on_undims_them(self):
        self.assertEqual(self.obs['ppOn']['gated'], self.obs['opened']['gated'])
        self.assertTrue(self.obs['ppOn']['emptyLinesWhenUngated'])

    def test_mkv_dims_the_three_encode_settings_and_nothing_else(self):
        pp_rows = {k: v for k, v in self.obs['mkv']['gated'].items() if k.startswith(PP)}
        line = 'Not used while Output format is MKV'
        self.assertEqual(pp_rows, {f'{PP}.reencode_mode': line, f'{PP}.video_crf': line,
                                   f'{PP}.audio_bitrate_kbps': line})

    def test_every_unmet_gate_is_named(self):
        self.assertEqual(self.obs['mkvNever']['gated'][f'{PP}.video_crf'],
                         'Not used while Output format is MKV and '
                         'Video re-encode (MP4 seek repair) is Never')

    def test_never_dims_only_the_crf(self):
        pp_rows = {k: v for k, v in self.obs['never']['gated'].items() if k.startswith(PP)}
        self.assertEqual(pp_rows, {f'{PP}.video_crf':
                                   'Not used while Video re-encode (MP4 seek repair) is Never'})

    def test_a_gated_row_that_matches_a_search_is_still_a_hit(self):
        self.assertEqual(self.obs['searched']['gatedHits'], [f'{PP}.video_crf'])
        self.assertIn(f'{PP}.video_crf', self.obs['searched']['shownPaths'])

    def test_the_restart_budget_and_the_speed_assumption_follow_their_own_switches(self):
        pp_rows = {k: v for k, v in self.obs['restartAndCollision']['gated'].items()
                   if k.startswith(PP)}
        self.assertEqual(pp_rows, {
            f'{PP}.max_restart_attempts': 'Not used while Auto-restart stopped conversions is off',
            f'{PP}.collision_lookahead_multiplier':
                'Not used while When a conversion collides with a recording is Off',
        })

    def test_move_on_complete_undims_its_destination(self):
        self.assertNotIn('recording.move_on_complete.destination', self.obs['moveOn']['gated'])


if __name__ == '__main__':
    unittest.main()
