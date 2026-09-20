"""Channel search's rows-per-page default (`search.page_size`, dev/changelog/1043).

The setting chooses what the search PAGE opens at. It must never change what the ENGINE
reads into a request with no `per_page`, because search URLs are an API that other pages
and stored links depend on: a link without a size has always meant 100 rows, and the page
spells `per_page` into its own address bar whenever it shows anything else.
"""
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app.channel_search import (DEFAULT_PAGE_SIZE, PAGE_SIZE_OPTIONS,  # noqa: E402
                                SearchState, configured_page_size)
from app.config import _DEFAULTS  # noqa: E402


class EngineMeaningTests(unittest.TestCase):

    def test_a_request_with_no_size_still_means_the_engine_default(self):
        state = SearchState.from_params({})
        self.assertEqual(state.page_size, DEFAULT_PAGE_SIZE)
        self.assertEqual(DEFAULT_PAGE_SIZE, 100)

    def test_a_non_default_size_is_spelled_into_the_link(self):
        state = SearchState.from_params({'per_page': '250'})
        self.assertIn(('per_page', '250'), state.to_params())

    def test_the_default_size_is_left_out_of_the_link(self):
        state = SearchState.from_params({'per_page': '100'})
        self.assertNotIn('per_page', [k for k, _ in state.to_params()])


class ConfiguredPageSizeTests(unittest.TestCase):

    def test_the_shipped_default_is_the_engine_default(self):
        self.assertEqual(_DEFAULTS['search']['page_size'], DEFAULT_PAGE_SIZE)
        self.assertEqual(configured_page_size({'search': {}}), DEFAULT_PAGE_SIZE)

    def test_every_menu_size_is_honored_in_either_spelling(self):
        for n in PAGE_SIZE_OPTIONS:
            self.assertEqual(configured_page_size({'search': {'page_size': n}}), n)
            self.assertEqual(configured_page_size({'search': {'page_size': str(n)}}), n)

    def test_an_off_menu_value_falls_back_and_says_so(self):
        for bad in (50, 'lots', None, True):
            with self.assertLogs('app.channel_search', 'WARNING'):
                self.assertEqual(configured_page_size({'search': {'page_size': bad}}),
                                 DEFAULT_PAGE_SIZE)


class CatalogTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _catalog(self, page_size):
        from app.config import load_config
        cfg = load_config()
        cfg['search']['page_size'] = page_size
        with mock.patch('app.routes.channel_search.load_config', return_value=cfg):
            resp = self.t.client.get('/api/channels/search/catalog')
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_the_catalog_carries_the_configured_opening_size(self):
        data = self._catalog(250)
        self.assertEqual(data['opening_page_size'], 250)
        self.assertEqual(data['page_size_options'], list(PAGE_SIZE_OPTIONS))

    def test_the_engine_default_in_the_catalog_does_not_move_with_the_setting(self):
        self.assertEqual(self._catalog(500)['page_size'], DEFAULT_PAGE_SIZE)


class SettingsSaveTests(unittest.TestCase):
    """Server-side, per CLAUDE.md enforcement-lives-server-side: the select only offers
    the menu, but both save paths must refuse anything else."""

    def setUp(self):
        self.t = make_test_app()
        page = self.t.client.get('/settings').get_data(as_text=True)
        self.tok = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)

    def tearDown(self):
        self.t.cleanup()

    def _save_field(self, value):
        with mock.patch('app.routes.settings.save_config', return_value=[]) as save, \
             mock.patch('app.routes.settings.load_for_edit',
                        return_value=({'search': {}}, {'search': {}})):
            resp = self.t.client.post('/api/settings/field',
                                      json={'path': 'search.page_size', 'value': value},
                                      headers={'X-CSRFToken': self.tok})
        return resp, save

    def test_a_menu_size_is_stored_as_a_number(self):
        resp, save = self._save_field('250')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(save.call_args[0][0]['search']['page_size'], 250)

    def test_an_off_menu_size_is_refused(self):
        resp, save = self._save_field('50')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())
        save.assert_not_called()

    def test_the_raw_yaml_editor_refuses_an_off_menu_size(self):
        with mock.patch('app.routes.settings.save_config', return_value=[]) as save:
            self.t.client.post('/settings', data={'config_yaml': 'search:\n  page_size: 75\n',
                                                  'csrf_token': self.tok})
        save.assert_not_called()

    def test_the_raw_yaml_editor_accepts_a_menu_size(self):
        with mock.patch('app.routes.settings.save_config', return_value=[]) as save:
            self.t.client.post('/settings', data={'config_yaml': 'search:\n  page_size: 500\n',
                                                  'csrf_token': self.tok})
        save.assert_called_once()

    def test_the_settings_page_renders_the_field_on_the_configured_value(self):
        page = self.t.client.get('/settings').get_data(as_text=True)
        m = re.search(r'<select data-setting-path="search\.page_size">(.*?)</select>', page, re.S)
        self.assertIsNotNone(m, 'the Rows per page field is not on the Settings page')
        self.assertEqual(re.findall(r'value="(\d+)"', m.group(1)),
                         [str(n) for n in PAGE_SIZE_OPTIONS])
        # The page reads the real config.yaml at run time, so which one is ticked is the
        # operator's; that exactly one is, is the page's.
        self.assertEqual(m.group(1).count(' selected'), 1)


if __name__ == '__main__':
    unittest.main()
