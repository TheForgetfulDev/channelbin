"""Gzipped XMLTV is imported, not silently discarded
(dev/docs/BUGS.md 2026-08-09, dev/changelog/523).

`_sync_epg_from_url` does `resp.content` and hands the raw bytes to `_import_xmltv`.
requests transparently decodes a response carrying `Content-Encoding: gzip`, but a
provider serving its guide as a `.xml.gz` FILE sends a file content type
(application/octet-stream) and no such header, so the compressed bytes arrived at the
parser unchanged. iterparse then found no <programme> elements and the sync reported a
perfectly healthy import of 0 entries - a permanently empty guide with nothing anywhere
naming the cause. Measured against a real provider: m3upt.com/epg is 2.6 MB gzipped,
31 MB of XMLTV, 248 channels and 41,689 programs, all of it previously dropped.

Decompression lives in `_import_xmltv` rather than `_sync_epg_from_url` so the Xtream
dump path gets it too, and so the collapse guard's count pass reads the same bytes the
real import loop does - which is what the CollapseGuardStillSeesTheContent case pins.

No network: requests.get is patched at app.accounts.requests.get. Throwaway temp SQLite.
  python3 -m unittest tests.test_epg_gzip
"""
import gzip
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import _import_xmltv, _maybe_gunzip, _sync_epg_from_url  # noqa: E402
from app.database import Channel, EPGEntry, M3uAccount  # noqa: E402
from tests.support import make_test_app  # noqa: E402


def _xmltv(channel_ids, per_channel=3):
    now = datetime.utcnow()
    parts = ['<?xml version="1.0" encoding="UTF-8"?><tv>']
    for chid in channel_ids:
        for n in range(per_channel):
            start = now + timedelta(hours=n)
            stop = start + timedelta(hours=1)
            parts.append(
                f'<programme start="{start.strftime("%Y%m%d%H%M%S")} +0000" '
                f'stop="{stop.strftime("%Y%m%d%H%M%S")} +0000" channel="{chid}">'
                f'<title>Show {n}</title></programme>'
            )
    parts.append('</tv>')
    return ''.join(parts).encode('utf-8')


class MaybeGunzipTests(unittest.TestCase):
    """The sniff itself, in isolation."""

    def test_plain_xml_passes_through_untouched(self):
        raw = _xmltv(['ch1'])
        out, reason = _maybe_gunzip(raw)
        self.assertIs(out, raw)
        self.assertIsNone(reason)

    def test_gzipped_payload_is_decompressed(self):
        raw = _xmltv(['ch1'])
        out, reason = _maybe_gunzip(gzip.compress(raw))
        self.assertEqual(out, raw)
        self.assertIsNone(reason)

    def test_truncated_gzip_is_reported_not_swallowed(self):
        """Magic bytes present but the stream is broken - that is a corrupt download, and
        it must surface as a degradation reason rather than as an empty-but-healthy guide."""
        broken = gzip.compress(_xmltv(['ch1']))[:20]
        out, reason = _maybe_gunzip(broken)
        self.assertIsNotNone(reason)
        self.assertIn('gzip', reason.lower())


class GzippedImportTests(unittest.TestCase):
    """End to end through _import_xmltv against a real temp DB."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.account = M3uAccount(name='Gz Provider', m3u_url='http://provider.test/p.m3u',
                                  epg_url='http://provider.test/guide.xml.gz')
        db.session.add(self.account)
        db.session.commit()
        for chid in ('ch1', 'ch2'):
            db.session.add(Channel(account_id=self.account.id, stream_id=abs(hash(chid)) % 100000,
                                   name=chid.upper(), epg_channel_id=chid,
                                   stream_url=f'http://provider.test/{chid}.ts'))
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_gzipped_xmltv_imports_the_same_entries_as_plain(self):
        raw = _xmltv(['ch1', 'ch2'], per_channel=3)
        n_gz, reason = _import_xmltv(self.account, gzip.compress(raw), epg_days=3)
        self.assertIsNone(reason)
        self.assertEqual(n_gz, 6)
        self.assertEqual(EPGEntry.query.count(), 6)

    def test_gzipped_fetch_through_sync_epg_from_url(self):
        """The real path: a provider serving .xml.gz with a file content type."""
        raw = _xmltv(['ch1', 'ch2'], per_channel=2)
        resp = mock.Mock()
        resp.content = gzip.compress(raw)
        resp.raise_for_status = mock.Mock()
        with mock.patch('app.accounts.requests.get', return_value=resp):
            n, reason = _sync_epg_from_url(self.account, self.account.epg_url, 30, 3)
        self.assertIsNone(reason)
        self.assertEqual(n, 4)

    def test_collapse_guard_sees_the_decompressed_content(self):
        """The guard's count pass parses xml_bytes separately from the import loop. If
        decompression happened after the guard, a gzipped payload would project 0 entries
        and the guard would refuse a perfectly good import."""
        self.account.epg_entry_count = 6
        db.session.commit()
        raw = _xmltv(['ch1', 'ch2'], per_channel=3)
        n, reason = _import_xmltv(self.account, gzip.compress(raw), epg_days=3)
        self.assertIsNone(reason, f'collapse guard wrongly refused: {reason}')
        self.assertEqual(n, 6)

    def test_corrupt_gzip_keeps_existing_epg(self):
        """A broken download must not run the delete - old EPG survives, sync reports why."""
        raw = _xmltv(['ch1'], per_channel=2)
        _import_xmltv(self.account, raw, epg_days=3)
        before = EPGEntry.query.count()
        self.assertEqual(before, 2)

        n, reason = _import_xmltv(self.account, gzip.compress(raw)[:20], epg_days=3)
        self.assertEqual(n, 0)
        self.assertIsNotNone(reason)
        self.assertEqual(EPGEntry.query.count(), before)


if __name__ == '__main__':
    unittest.main()
