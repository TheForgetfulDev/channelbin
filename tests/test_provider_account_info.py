"""Capturing the Xtream auth response's user_info block onto Account.provider_*
(dev/changelog/534) - previously read into a local var and discarded every sync.

Three layers: the epoch-string parser (tz_utils.parse_epoch_utc), the payload parser
(accounts.parse_provider_account_info) against real-shaped and degenerate inputs, and the
real _do_sync path via FileXtreamClient against dump files (same harness as
test_account_sync_added_removed.py) to prove the values actually land on the Account row.

  python3 tests/test_provider_account_info.py
"""
import json
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import NORM_MPEGTS_LIVE, _do_sync, parse_provider_account_info  # noqa: E402
from app.config import _deep_merge, load_config  # noqa: E402
from app.database import XtreamAccount  # noqa: E402
from app.tz_utils import parse_epoch_utc  # noqa: E402
from tests.support import make_test_app  # noqa: E402

BASE_URL = 'http://provider.test:8080'
USERNAME = 'testuser'
PASSWORD = 'testpass'

# Shaped like the real sample (instance/xtream-dumps/2/2026-08-07_01/auth.json): every
# user_info numeric/boolean field is a JSON string, not a native type.
REAL_SHAPED_AUTH = {
    'user_info': {
        'playlist_name': 'Test Playlist', 'username': USERNAME, 'password': PASSWORD,
        'message': None, 'auth': 1, 'status': 'Active',
        'exp_date': '1798153200', 'is_trial': '0', 'active_cons': '2',
        'created_at': '1766695914', 'max_connections': '3',
        'allowed_output_formats': ['m3u8', 'ts', 'rtmp'],
    },
    'server_info': {
        'url': 'stream.provider.test', 'port': '80', 'https_port': '443',
        'server_protocol': 'http', 'timezone': 'UTC',
    },
}


class ParseEpochUtcTests(unittest.TestCase):
    def test_valid_epoch_string(self):
        dt = parse_epoch_utc('1798153200')
        self.assertEqual(dt.year, 2026)

    def test_valid_epoch_int(self):
        self.assertIsNotNone(parse_epoch_utc(1798153200))

    def test_none_returns_none(self):
        self.assertIsNone(parse_epoch_utc(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(parse_epoch_utc(''))

    def test_non_numeric_string_returns_none(self):
        self.assertIsNone(parse_epoch_utc('not-a-timestamp'))


class ParseProviderAccountInfoTests(unittest.TestCase):
    def test_real_shaped_payload(self):
        info = parse_provider_account_info(REAL_SHAPED_AUTH)
        self.assertEqual(info['status'], 'Active')
        self.assertEqual(info['exp_date'].year, 2026)
        self.assertIs(info['is_trial'], False)
        self.assertEqual(info['max_connections'], 3)
        self.assertEqual(info['active_connections'], 2)
        self.assertEqual(info['allowed_output_formats'], 'm3u8,ts,rtmp')

    def test_is_trial_true(self):
        payload = {'user_info': {'is_trial': '1'}}
        self.assertIs(parse_provider_account_info(payload)['is_trial'], True)

    def test_m3u_for_auth_synthesized_payload_is_all_none(self):
        """The non-standard M3U-for-auth path synthesizes {'user_info': {'auth': 1}} - no
        other field. Every value must come back None, not raise."""
        info = parse_provider_account_info({'user_info': {'auth': 1}})
        self.assertEqual(info, {
            'status': None, 'exp_date': None, 'is_trial': None,
            'max_connections': None, 'active_connections': None,
            'allowed_output_formats': None,
        })

    def test_empty_payload_is_all_none(self):
        self.assertEqual(parse_provider_account_info({}), {
            'status': None, 'exp_date': None, 'is_trial': None,
            'max_connections': None, 'active_connections': None,
            'allowed_output_formats': None,
        })

    def test_none_payload_is_all_none(self):
        info = parse_provider_account_info(None)
        self.assertIsNone(info['status'])

    def test_allowed_output_formats_not_a_list_is_none(self):
        """A provider echoing junk (a string, not a list) must degrade gracefully rather
        than raise or silently join characters."""
        payload = {'user_info': {'allowed_output_formats': 'ts'}}
        self.assertIsNone(parse_provider_account_info(payload)['allowed_output_formats'])


class DoSyncPersistsProviderInfoTests(unittest.TestCase):
    """Drives the real _do_sync path via FileXtreamClient against dump files - proves the
    parsed fields actually reach the Account row, not just the parser function."""

    def setUp(self):
        self.t = make_test_app()
        self.dump_base = os.path.join(self.t._tmpdir, 'xtream_dumps')
        self.account = XtreamAccount(
            name='Provider Info Test', base_url=BASE_URL,
            username=USERNAME, password=PASSWORD, status='OK',
            url_normalization=NORM_MPEGTS_LIVE, max_connections=1)
        db.session.add(self.account)
        db.session.flush()

    def tearDown(self):
        self.t.cleanup()

    def _write_dump(self, auth_payload):
        d = os.path.join(self.dump_base, str(self.account.id), '2026-01-01_01')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'auth.json'), 'w', encoding='utf-8') as f:
            json.dump(auth_payload, f)
        with open(os.path.join(d, 'live_streams_json_api.json'), 'w', encoding='utf-8') as f:
            json.dump([{'stream_id': 1, 'name': 'Channel 1', 'stream_icon': '',
                       'category_id': '1', 'epg_channel_id': 'ch1.test'}], f)

    def _sync(self):
        cfg = _deep_merge(load_config(), {'debug': {'xtream_dump_dir': self.dump_base}})
        with mock.patch('app.accounts.load_config', return_value=cfg):
            _do_sync(self.account.id, threading.Event(), use_dump=True)
        db.session.expire_all()

    def test_provider_fields_land_on_the_account_row(self):
        self._write_dump(REAL_SHAPED_AUTH)
        self._sync()

        acc = db.session.get(XtreamAccount, self.account.id)
        self.assertEqual(acc.provider_status, 'Active')
        self.assertEqual(acc.provider_exp_date.year, 2026)
        self.assertIs(acc.provider_is_trial, False)
        self.assertEqual(acc.provider_max_connections, 3)
        self.assertEqual(acc.provider_active_connections, 2)
        self.assertEqual(acc.provider_allowed_output_formats, 'm3u8,ts,rtmp')
        # stream_origin_from_server_info reproduces the port verbatim, including a
        # default one - see its own docstring for why.
        self.assertEqual(acc.provider_stream_origin, 'http://stream.provider.test:80')

    def test_degenerate_auth_response_leaves_fields_none_not_raising(self):
        self._write_dump({'user_info': {'auth': 1, 'status': 'Active'}})
        self._sync()

        acc = db.session.get(XtreamAccount, self.account.id)
        self.assertEqual(acc.provider_status, 'Active')
        self.assertIsNone(acc.provider_exp_date)
        self.assertIsNone(acc.provider_is_trial)
        self.assertIsNone(acc.provider_max_connections)
        self.assertIsNone(acc.provider_stream_origin, 'no server_info key in this dump')


if __name__ == '__main__':
    unittest.main(verbosity=2)
