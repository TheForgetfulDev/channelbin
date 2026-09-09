"""Tier 2 - mass URL-rewrite alert on sync (URL drift 3/3).

Guards DESIGN-url-drift.md §"URL drift 3/3": when a provider moves its stream domain or
rotates the credentials embedded in every stream URL, one sync silently rewrites thousands
of Channel.stream_url values. Channel identity survives (matching is stream_id-keyed), so
nothing breaks - but the user has no way to know it happened. A WARN alert makes it
observable.

Hard constraints this pins, all from the design's Decisions section:
  * informational only - the sync always succeeds and no channel work is skipped;
  * threshold-gated (`sync.url_drift_alert_min_channels`, 0 disables);
  * no plaintext credentials in the alert body;
  * a standing alert is refreshed, not stacked, while the drift keeps recurring.

Drives a real _do_sync() over a FileXtreamClient dump (no network - see CLAUDE.md
§Testing); the "provider moved" event is modelled by editing the account's base_url and
password between syncs, which is exactly what re-points every constructed stream URL.

Run standalone:
  python3 -m unittest tests.test_url_drift_alert
"""
import json
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import NORM_MPEGTS_LIVE, _do_sync  # noqa: E402
from app.config import _deep_merge, load_config  # noqa: E402
from app.database import Alert, Channel, XtreamAccount  # noqa: E402
from tests.support import make_test_app  # noqa: E402
from tests.support.seed import make_channel  # noqa: E402

BASE_URL = 'http://provider.test:8080'
NEW_BASE_URL = 'http://newcdn.test:8080'
USERNAME = 'testuser'
PASSWORD = 'sup3rsecret'
NEW_PASSWORD = 'r0tatedsecret'
CHANNEL_COUNT = 6


def _stream(sid):
    """One Xtream live-stream dict as the JSON API returns it. No _stream_url, so
    _upsert_channels constructs the URL from the account credentials - which is what makes
    a base_url/password edit behave like a provider-side URL rewrite."""
    return {
        'stream_id': sid,
        'name': f'Channel {sid}',
        'category_id': '1',
        'epg_channel_id': f'ch{sid}.test',
    }


class UrlDriftAlertTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.dump_base = os.path.join(self.t._tmpdir, 'xtream_dumps')
        self.dump_seq = 0

        self.account = XtreamAccount(
            name='Drift Provider', base_url=BASE_URL,
            username=USERNAME, password=PASSWORD, status='OK',
            # These streams carry no _stream_url, so every URL here is CONSTRUCTED, and
            # construction renders the account's mode - it cannot be Disabled or there is
            # no form to build in. Pinned to the mode matching the URLs asserted below;
            # this previously read `False` and only worked by accident, resolving through
            # the real config.yaml because a bool round-trips out of the (String) column
            # as 0 rather than False.
            url_normalization=NORM_MPEGTS_LIVE)
        db.session.add(self.account)
        db.session.flush()
        self.channels = [make_channel(self.account, stream_id=i, name=f'Channel {i}')
                         for i in range(1, CHANNEL_COUNT + 1)]
        db.session.commit()

        self._write_dump()
        # Sync once at the original URLs so every channel row holds a raw_stream_url the
        # provider then drifts away from.
        self._sync()

    def tearDown(self):
        self.t.cleanup()

    def _write_dump(self):
        self.dump_seq += 1
        d = os.path.join(self.dump_base, str(self.account.id), f'2026-01-01_{self.dump_seq:02d}')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'auth.json'), 'w', encoding='utf-8') as f:
            json.dump({'user_info': {'auth': 1, 'status': 'Active'}}, f)
        with open(os.path.join(d, 'live_streams_json_api.json'), 'w', encoding='utf-8') as f:
            json.dump([_stream(sid) for sid in range(1, CHANNEL_COUNT + 1)], f)
        # No xmltv.xml on purpose - keeps the EPG branch off the network.

    def _sync(self, threshold=None):
        overrides = {'debug': {'xtream_dump_dir': self.dump_base}}
        if threshold is not None:
            overrides['sync'] = {'url_drift_alert_min_channels': threshold}
        cfg = _deep_merge(load_config(), overrides)
        with mock.patch('app.accounts.load_config', return_value=cfg):
            _do_sync(self.account.id, threading.Event(), use_dump=True)
        db.session.expire_all()

    def _drift_provider(self, base_url=NEW_BASE_URL, password=NEW_PASSWORD):
        self.account.base_url = base_url
        self.account.password = password
        db.session.commit()
        self._write_dump()

    def _alerts(self):
        return Alert.query.filter_by(alert_type='PROVIDER_URLS_CHANGED').all()

    def test_drift_over_threshold_raises_one_alert(self):
        self._drift_provider()
        self._sync(threshold=CHANNEL_COUNT)

        alerts = self._alerts()
        self.assertEqual(len(alerts), 1, 'expected exactly one drift alert')
        self.assertEqual(alerts[0].severity, 'WARN')
        self.assertEqual(alerts[0].source, f'account:{self.account.id}:url-drift')
        self.assertIn(str(CHANNEL_COUNT), alerts[0].title)

    def test_drift_under_threshold_raises_nothing(self):
        """CONTROL for the threshold gate - asserts absence, so it also passes with the
        feature removed entirely."""
        self._drift_provider()
        self._sync(threshold=CHANNEL_COUNT + 1)
        self.assertEqual(self._alerts(), [])

    def test_threshold_zero_disables_the_alert(self):
        """CONTROL - asserts absence (see test_drift_under_threshold_raises_nothing)."""
        self._drift_provider()
        self._sync(threshold=0)
        self.assertEqual(self._alerts(), [])

    def test_no_drift_raises_nothing(self):
        """CONTROL - asserts absence.

        A sync that rewrites nothing must stay silent even at threshold 1."""
        self._write_dump()
        self._sync(threshold=1)
        self.assertEqual(self._alerts(), [])

    def test_alert_body_masks_credentials(self):
        self._drift_provider()
        self._sync(threshold=1)

        alert = self._alerts()[0]
        blob = f'{alert.title}\n{alert.body}'
        self.assertNotIn(PASSWORD, blob, 'old password leaked into the alert')
        self.assertNotIn(NEW_PASSWORD, blob, 'rotated password leaked into the alert')
        self.assertNotIn(USERNAME, blob, 'username leaked into the alert')

    def test_repeat_drift_refreshes_instead_of_stacking(self):
        self._drift_provider()
        self._sync(threshold=1)
        first = self._alerts()[0]
        first_id, first_created = first.id, first.created_at
        first.read_at = first_created
        db.session.commit()

        self._drift_provider(base_url='http://thirdcdn.test:8080', password='another')
        self._sync(threshold=1)

        alerts = self._alerts()
        self.assertEqual(len(alerts), 1, 'a second drift stacked a new alert row')
        self.assertEqual(alerts[0].id, first_id, 'the standing alert row must be reused')
        self.assertGreaterEqual(alerts[0].created_at, first_created)
        self.assertIsNone(alerts[0].read_at,
                          'a fresh mass rewrite must resurface as unread')

    def test_sync_still_succeeds_and_imports_during_drift(self):
        """CHARACTERIZATION - the sync already succeeded before the drift alert existed. It pins the
        design's hard rule that the alert is observational and never becomes a gate."""
        self._drift_provider()
        self._sync(threshold=1)

        self.assertEqual(self.account.status, 'OK')
        self.assertEqual(self.account.channel_count, CHANNEL_COUNT)
        ch = db.session.get(Channel, self.channels[0].id)
        self.assertEqual(
            ch.raw_stream_url,
            f'{NEW_BASE_URL}/live/{USERNAME}/{NEW_PASSWORD}/1.ts',
            'channels must still be repointed at the new provider URL')

    def test_dismissed_alert_does_not_block_a_new_one(self):
        """Refresh targets undismissed rows only - once the user dismisses, the next mass
        drift is news again."""
        self._drift_provider()
        self._sync(threshold=1)
        from datetime import datetime
        self._alerts()[0].dismissed_at = datetime.utcnow()
        db.session.commit()

        self._drift_provider(base_url='http://fourthcdn.test:8080', password='yetanother')
        self._sync(threshold=1)

        alerts = self._alerts()
        self.assertEqual(len(alerts), 2)
        self.assertEqual(len([a for a in alerts if a.dismissed_at is None]), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
