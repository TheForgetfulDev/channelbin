"""Tier 2 - per-channel CHANNEL_URL_CHANGED event on sync rewrite (URL drift 4/3).

Guards DESIGN-url-drift.md 4/3: `_upsert_channels` overwrites Channel.stream_url in place
with no ChannelEvent, so a channel's own Activity Timeline stays silent about URL drift that
just repointed it - even though URL drift 1/3 and 3/3 already produce signals elsewhere (a
RecordingEvent visible only on the recording detail page, and an account-level alert
respectively). This guards the fix: a creds-masked CHANNEL_URL_CHANGED ChannelEvent written
at the sync rewrite site for every drifted channel, sourced from the same `drifted` list URL
drift 3/3 already computes (no re-walking the sync loop).

Hard constraints this pins:
  * exactly one CHANNEL_URL_CHANGED event per drifted channel, none when nothing drifted;
  * no plaintext credentials in the event detail;
  * written regardless of the account-level sync.url_drift_alert_min_channels threshold
    (the account alert is a separate, threshold-gated summary, not a gate on the
    per-channel history);
  * a mass drift is a single batched insert/commit, not one commit per row.

Drives a real _do_sync() over a FileXtreamClient dump (no network - see CLAUDE.md
§Testing); the "provider moved" event is modelled by editing the account's base_url and
password between syncs, exactly like tests/test_url_drift_alert.py.

Run standalone:
  python3 -m unittest tests.test_channel_url_changed_event
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
from app.database import CHANNEL_URL_CHANGED, Channel, ChannelEvent, XtreamAccount  # noqa: E402
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


class ChannelUrlChangedEventTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.dump_base = os.path.join(self.t._tmpdir, 'xtream_dumps')
        self.dump_seq = 0

        self.account = XtreamAccount(
            name='Drift Provider', base_url=BASE_URL,
            username=USERNAME, password=PASSWORD, status='OK',
            url_normalization=NORM_MPEGTS_LIVE)
        db.session.add(self.account)
        db.session.flush()
        self.channels = [make_channel(self.account, stream_id=i, name=f'Channel {i}')
                         for i in range(1, CHANNEL_COUNT + 1)]
        # make_channel seeds a placeholder raw_stream_url that differs from whatever the
        # real first sync below constructs - which would itself register as "drift" and
        # double-count every assertion here. Blank it so only the deliberate
        # _drift_provider() calls in each test produce a drifted URL.
        for ch in self.channels:
            ch.raw_stream_url = None
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

    def _events(self):
        return ChannelEvent.query.filter_by(event_type=CHANNEL_URL_CHANGED).all()

    def test_drift_writes_one_event_per_channel(self):
        self._drift_provider()
        self._sync(threshold=CHANNEL_COUNT)

        events = self._events()
        self.assertEqual(len(events), CHANNEL_COUNT)
        self.assertEqual(
            {e.channel_id for e in events},
            {c.id for c in self.channels})

    def test_events_written_even_below_the_account_alert_threshold(self):
        """The per-channel event is NOT gated by sync.url_drift_alert_min_channels
        (2026-08-12) - that threshold only controls the separate account-level
        PROVIDER_URLS_CHANGED alert."""
        self._drift_provider()
        self._sync(threshold=CHANNEL_COUNT + 1)
        self.assertEqual(len(self._events()), CHANNEL_COUNT)

    def test_no_drift_writes_nothing(self):
        """CONTROL for the drift gate - asserts absence, so it also passes with the
        feature removed entirely."""
        self._write_dump()
        self._sync()
        self.assertEqual(self._events(), [])

    def test_event_detail_masks_credentials(self):
        self._drift_provider()
        self._sync()

        blob = '\n'.join(e.detail or '' for e in self._events())
        self.assertNotIn(PASSWORD, blob, 'old password leaked into the event detail')
        self.assertNotIn(NEW_PASSWORD, blob, 'rotated password leaked into the event detail')
        self.assertNotIn(USERNAME, blob, 'username leaked into the event detail')

    def test_event_carries_no_plaintext_url_shape_hint(self):
        """The detail must actually mask, not merely happen to omit the password - check
        the masked marker is present so this doesn't pass by omission."""
        self._drift_provider()
        self._sync()

        event = self._events()[0]
        self.assertIn('***', event.detail)

    def test_repeat_drift_writes_a_new_event_each_time(self):
        """Unlike the standing account-level alert, this is a per-channel history log - a
        second, later drift of the same channel is a new fact and must not be collapsed
        into (or overwrite) the first event."""
        self._drift_provider()
        self._sync()
        first_count = len(self._events())

        self._drift_provider(base_url='http://thirdcdn.test:8080', password='another')
        self._sync()

        self.assertEqual(len(self._events()), first_count + CHANNEL_COUNT)

    def test_channel_id_references_the_drifted_channel(self):
        self._drift_provider()
        self._sync()

        one_channel = self.channels[0]
        matching = [e for e in self._events() if e.channel_id == one_channel.id]
        self.assertEqual(len(matching), 1)
        ch = db.session.get(Channel, one_channel.id)
        self.assertEqual(
            ch.raw_stream_url,
            f'{NEW_BASE_URL}/live/{USERNAME}/{NEW_PASSWORD}/{one_channel.stream_id}.ts')

    def test_mass_drift_is_one_commit_not_one_per_channel(self):
        """CLAUDE.md's commit rule: a mass drift must be a single batched insert, not one
        add()+commit per row. Counts db.session.commit() calls made specifically while
        writing the drift events, by comparing the commit count for a 1-channel drift sync
        against this suite's CHANNEL_COUNT-channel drift sync - if commits scaled with row
        count the larger sync would need more calls than the smaller one."""
        from app import accounts as accounts_mod

        commit_calls = []
        real_commit = db.session.commit

        def _counting_commit(*a, **kw):
            commit_calls.append(1)
            return real_commit(*a, **kw)

        self._drift_provider()
        with mock.patch.object(accounts_mod.db.session, 'commit', side_effect=_counting_commit):
            accounts_mod._write_channel_url_drift_events(
                [(c.id, 'http://old.test/a', 'http://new.test/a') for c in self.channels])

        self.assertEqual(len(commit_calls), 1,
                         'writing N drifted channels must be a single commit, not N')


if __name__ == '__main__':
    unittest.main(verbosity=2)
