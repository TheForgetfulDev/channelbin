"""Tier 1 pure units for URL normalization (app/accounts.py::normalize_url /
normalize_url_loose). No Flask, no DB - a fake account object is enough.

Also pins the per-row-loop hoist half of BUGS.md 2026-07-15 10:34: when an account
defers to the global default (url_normalization is None) but a pre-loaded cfg is passed,
normalize_url must NOT touch disk (no load_config call).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.accounts as accounts_mod  # noqa: E402
from app.accounts import normalize_url, normalize_url_loose  # noqa: E402


class FakeAccount:
    def __init__(self, url_normalization=None):
        self.url_normalization = url_normalization


class NormalizeUrlLooseTests(unittest.TestCase):
    def test_strips_live_prefix(self):
        self.assertEqual(normalize_url_loose('http://h/live/1234'), 'http://h/1234')

    def test_strips_ts_extension(self):
        self.assertEqual(normalize_url_loose('http://h/live/1234.ts'), 'http://h/1234')

    def test_strips_m3u8_extension(self):
        self.assertEqual(normalize_url_loose('http://h/live/1234.m3u8'), 'http://h/1234')

    def test_leaves_plain_url_untouched(self):
        self.assertEqual(normalize_url_loose('http://h/stream/1234'), 'http://h/stream/1234')

    def test_only_trailing_extension_stripped(self):
        # a .ts mid-path must not be stripped - anchored to end of string
        self.assertEqual(normalize_url_loose('http://h/a.ts/b'), 'http://h/a.ts/b')


class NormalizeUrlAccountTests(unittest.TestCase):
    # A realistic provider URL: every one of the three spellings carries a
    # user/password/id triplet. The pre-_m014 fixture here was 'http://h/live/1234.ts',
    # which has no user or password and matches no real provider URL - and, as the
    # production data showed, an id-only path in the wild is an Icecast radio mount
    # (e.g. icecast.example:8000/904) where rewriting it to /live/AAA/BBB/904.ts would
    # destroy a working stream. Such URLs are deliberately left untouched now.
    URL = 'http://h/live/AAA/BBB/1234.ts'

    def test_account_override_true_normalizes(self):
        """Legacy boolean True keeps meaning what it used to: the without-live form."""
        self.assertEqual(normalize_url(self.URL, FakeAccount(url_normalization=True)),
                         'http://h/AAA/BBB/1234')

    def test_account_override_false_passes_through(self):
        self.assertEqual(normalize_url(self.URL, FakeAccount(url_normalization=False)), self.URL)

    def test_none_defers_to_cfg_default_true(self):
        cfg = {'sync': {'url_normalization': True}}
        self.assertEqual(normalize_url(self.URL, FakeAccount(None), cfg),
                         'http://h/AAA/BBB/1234')

    def test_none_defers_to_cfg_default_false(self):
        cfg = {'sync': {'url_normalization': False}}
        self.assertEqual(normalize_url(self.URL, FakeAccount(None), cfg), self.URL)

    def test_passed_cfg_avoids_disk_load_config(self):
        """BUGS.md 2026-07-15 10:34 (per-row-loop class): passing a pre-loaded cfg for a
        default-deferring account must not call load_config() - the hoist that keeps the
        guide/search routes O(1) in config reads regardless of row count."""
        original = accounts_mod.load_config
        calls = []
        accounts_mod.load_config = lambda *a, **k: calls.append(1) or {'sync': {}}
        try:
            normalize_url(self.URL, FakeAccount(None), {'sync': {'url_normalization': True}})
        finally:
            accounts_mod.load_config = original
        self.assertEqual(calls, [], 'normalize_url read config from disk despite a passed cfg')


if __name__ == '__main__':
    unittest.main(verbosity=2)
