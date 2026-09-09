"""Tier 2 - re-normalize existing channel URLs on demand (dev/changelog/531).

Changing an account's URL Normalization mode only reshaped stream URLs for channels touched
by a LATER sync; existing channels kept whatever shape they were imported under. This is the
route (`POST /api/accounts/<id>/renormalize-urls`) that recomputes stream_url for every
existing channel from its stored raw_stream_url under the account's current mode, with no
provider connection - see app/routes/accounts.py::renormalize_urls_api.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import make_test_app  # noqa: E402
from app import db  # noqa: E402
from app.accounts import NORM_DISABLED, NORM_HLS, NORM_MPEGTS, NORM_MPEGTS_LIVE  # noqa: E402
from app.database import Account, Channel  # noqa: E402
from app.routes.accounts import _DELETE_CHUNK_SIZE  # noqa: E402


def _make_account(**kw):
    kw.setdefault('base_url', '')
    kw.setdefault('username', '')
    kw.setdefault('password', '')
    acc = Account(name='Test Account', account_type='m3u', status='OK', **kw)
    db.session.add(acc)
    db.session.flush()
    return acc


def _make_channel(account, sid, raw_url, stream_url=None, url_normalizable=True):
    ch = Channel(
        account_id=account.id, stream_id=sid, name=f'Ch {sid}',
        raw_stream_url=raw_url, stream_url=stream_url or raw_url,
        url_normalizable=url_normalizable,
    )
    db.session.add(ch)
    return ch


class RenormalizeUrlsApiTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False

    def tearDown(self):
        self.t.cleanup()

    def test_rewrites_every_channel_to_the_new_mode(self):
        acct = _make_account(url_normalization=NORM_MPEGTS)
        _make_channel(acct, 1, 'http://cdn.example/U/P/1.ts',
                      stream_url='http://cdn.example/U/P/1.ts')
        _make_channel(acct, 2, 'http://cdn.example/U/P/2.m3u8',
                      stream_url='http://cdn.example/U/P/2.m3u8')
        db.session.commit()

        resp = self.t.client.post(f'/api/accounts/{acct.id}/renormalize-urls')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['changed_count'], 2)
        self.assertEqual(data['total_count'], 2)

        db.session.expire_all()
        urls = {ch.stream_id: ch.stream_url for ch in Channel.query.filter_by(account_id=acct.id)}
        self.assertEqual(urls[1], 'http://cdn.example/U/P/1')
        self.assertEqual(urls[2], 'http://cdn.example/U/P/2')

    def test_disabled_mode_is_rejected_without_touching_rows(self):
        # An explicit account-level override, not "defer to the global default" - the route
        # reads load_config() at RUNTIME (CLAUDE.md: make_test_app overrides aren't visible
        # to it), so this must not depend on whatever sync.url_normalization the real
        # config.yaml on the machine running the suite happens to have.
        acct = _make_account(url_normalization=NORM_DISABLED)
        _make_channel(acct, 1, 'http://cdn.example/U/P/1.ts')
        db.session.commit()

        resp = self.t.client.post(f'/api/accounts/{acct.id}/renormalize-urls')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())

        db.session.expire_all()
        ch = Channel.query.filter_by(account_id=acct.id).first()
        self.assertEqual(ch.stream_url, 'http://cdn.example/U/P/1.ts')

    def test_no_triplet_url_is_left_untouched_and_not_counted(self):
        acct = _make_account(url_normalization=NORM_HLS)
        _make_channel(acct, 1, 'https://cdn.example/static/radio.mp3',
                      stream_url='https://cdn.example/static/radio.mp3',
                      url_normalizable=False)
        db.session.commit()

        resp = self.t.client.post(f'/api/accounts/{acct.id}/renormalize-urls')
        data = resp.get_json()
        self.assertEqual(data['changed_count'], 0)
        self.assertEqual(data['total_count'], 1)

        db.session.expire_all()
        ch = Channel.query.filter_by(account_id=acct.id).first()
        self.assertEqual(ch.stream_url, 'https://cdn.example/static/radio.mp3')

    def test_second_call_is_a_no_op(self):
        acct = _make_account(url_normalization=NORM_MPEGTS_LIVE)
        _make_channel(acct, 1, 'http://cdn.example/U/P/1', stream_url='http://cdn.example/U/P/1')
        db.session.commit()

        first = self.t.client.post(f'/api/accounts/{acct.id}/renormalize-urls').get_json()
        self.assertEqual(first['changed_count'], 1)

        second = self.t.client.post(f'/api/accounts/{acct.id}/renormalize-urls').get_json()
        self.assertEqual(second['changed_count'], 0)

    def test_url_normalizable_flag_is_corrected(self):
        """A channel stamped wrong (e.g. by an older bug) gets its flag fixed alongside the
        URL rewrite - both are derived from the same raw_stream_url, in the same pass."""
        acct = _make_account(url_normalization=NORM_MPEGTS)
        _make_channel(acct, 1, 'http://cdn.example/U/P/1.ts',
                      stream_url='http://cdn.example/U/P/1.ts', url_normalizable=False)
        db.session.commit()

        self.t.client.post(f'/api/accounts/{acct.id}/renormalize-urls')

        db.session.expire_all()
        ch = Channel.query.filter_by(account_id=acct.id).first()
        self.assertTrue(ch.url_normalizable)

    def test_chunking_covers_more_than_one_chunk(self):
        """Seed one more row than a single IN(...) chunk to prove the loop actually chunks
        rather than only being exercised at n=1 (app/routes/channels.py::_DELETE_CHUNK_SIZE,
        reused here for the same "keep every statement under SQLITE_MAX_VARIABLE_NUMBER"
        concern)."""
        acct = _make_account(url_normalization=NORM_MPEGTS)
        total = _DELETE_CHUNK_SIZE + 5
        for i in range(total):
            _make_channel(acct, i + 1, f'http://cdn.example/U/P/{i + 1}.ts',
                          stream_url=f'http://cdn.example/U/P/{i + 1}.ts')
        db.session.commit()

        resp = self.t.client.post(f'/api/accounts/{acct.id}/renormalize-urls')
        data = resp.get_json()
        self.assertEqual(data['changed_count'], total)
        self.assertEqual(data['total_count'], total)

    def test_unknown_account_404s(self):
        resp = self.t.client.post('/api/accounts/999999/renormalize-urls')
        self.assertEqual(resp.status_code, 404)


if __name__ == '__main__':
    unittest.main(verbosity=2)
