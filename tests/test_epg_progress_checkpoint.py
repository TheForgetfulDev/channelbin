"""EPG import progress checkpoint (dev/docs/BUGS.md 2026-08-16).

The EPG phase's progress checkpoint used to fire only on `synced % 500 == 0`, but `synced`
advances by `len(channel_ids)` per programme, so a feed where most programmes map to multiple
channels can jump straight past an exact multiple of 500 and stall visible progress for long
stretches. The fix checks the *distance* since the last checkpoint
(`synced - last_checkpoint >= 500`) instead of requiring an exact hit.

No network: runs `_import_xmltv` directly against a throwaway temp SQLite DB - never dvr.db.
  python3 -m unittest tests.test_epg_progress_checkpoint
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import _import_xmltv  # noqa: E402
from app.database import Channel, M3uAccount  # noqa: E402
from tests.support import make_test_app  # noqa: E402

M3U_URL = 'http://provider.test/playlist.m3u8?user=realuser&pass=realpass'


def _xmltv_multi_channel(epg_channel_id, count):
    """count <programme> elements, all mapped to the same shared epg_channel_id, well inside
    the default 3-day import window."""
    now = datetime.utcnow()
    parts = ['<?xml version="1.0" encoding="UTF-8"?><tv>']
    for i in range(count):
        start = now + timedelta(minutes=i)
        stop = start + timedelta(minutes=1)
        parts.append(
            f'<programme start="{start.strftime("%Y%m%d%H%M%S")} +0000" '
            f'stop="{stop.strftime("%Y%m%d%H%M%S")} +0000" channel="{epg_channel_id}">'
            f'<title>Show {i}</title></programme>'
        )
    parts.append('</tv>')
    return ''.join(parts).encode('utf-8')


class ProgressCheckpointTests(unittest.TestCase):
    """A feed where every programme maps to 3 channels advances `synced` by 3 each time, so an
    exact `% 500 == 0` check never lands on the checkpoint - it must fire once the gap since
    the last checkpoint reaches 500, not only on an exact multiple of 500."""

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Progress Test', m3u_url=M3U_URL, status='OK')
        db.session.add(self.account)
        db.session.flush()
        self.channels = [
            Channel(
                account_id=self.account.id, stream_id=i, name=f'Ch{i}',
                stream_url=f'http://example.test/live/{i}', epg_channel_id='shared.test',
            )
            for i in range(1, 4)
        ]
        db.session.add_all(self.channels)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_checkpoint_fires_on_distance_not_exact_multiple(self):
        # 200 programmes x 3 channels = 600 synced entries. 500 is not a multiple of 3, so the
        # old exact-modulo check never lands on a checkpoint mid-parse - only the unconditional
        # call after the loop fires, at the very end.
        xml = _xmltv_multi_channel('shared.test', 200)
        calls = []
        with mock.patch('app.accounts._set_sync_progress',
                         side_effect=lambda *a: calls.append(a)):
            synced, reason = _import_xmltv(
                self.account, xml, epg_days=3,
                cfg={'sync': {'epg_collapse_threshold_percent': 0}})

        self.assertIsNone(reason)
        self.assertEqual(synced, 600)
        epg_calls = [c for c in calls if c[1] == 'epg']
        self.assertGreaterEqual(
            len(epg_calls), 2,
            'must checkpoint at least once mid-parse, not just the final unconditional call')
        first_done = epg_calls[0][2]
        self.assertLess(first_done, 600, 'the first checkpoint must be a genuine mid-parse one')
        self.assertGreaterEqual(
            first_done, 500, 'checkpoint must fire once 500 have accumulated since the last one')


if __name__ == '__main__':
    unittest.main()
