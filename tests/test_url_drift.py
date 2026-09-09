"""Tier 2 - URL drift 1/3 (DESIGN-url-drift.md; BUGS.md 2026-07-20 "stale Recording.url").

A channel-backed recording's `url` is frozen at creation. When the provider rewrites its
stream domain and/or the creds embedded in every stream URL, sync repairs the Channel row in
place (identity matches on stream_id, not URL) but leaves every already-scheduled recording
pointing at the dead old URL. `_reresolve_channel_url` repoints it from the channel at every
ffmpeg launch - record start, service-restart resume, and each watchdog segment relaunch.

Guards asserted here:
  * a stale channel-backed recording launches ffmpeg against the channel's CURRENT
    stream_url and logs one RECORDING_URL_RERESOLVED event with both URLs creds-masked;
  * no event (and no write) when the URL already matches - the common case;
  * group-backed and manual URL-only recordings are never touched.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_url_drift
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_group, make_recording  # noqa: E402

from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import Recording, RecordingEvent, RECORDING_URL_RERESOLVED  # noqa: E402
from app.recorder import _launch_segment, _reresolve_channel_url  # noqa: E402

STALE = 'http://old-domain.test/live/olduser/oldpass/4242'


class UrlDriftTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        # url_normalization pinned per-account: make_test_app's overrides are NOT visible to
        # the runtime load_config() these paths call, so leaving it None would make every
        # assertion depend on the real config.yaml (CLAUDE.md §Testing).
        self.account = make_account(url_normalization=False)
        self.channel = make_channel(self.account, stream_id=4242, name='Drifted Channel')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _events(self, rec_id):
        return RecordingEvent.query.filter_by(
            recording_id=rec_id, event_type=RECORDING_URL_RERESOLVED).all()

    def test_stale_channel_url_is_repointed_and_logged(self):
        rec = make_recording(channel_id=self.channel.id, url=STALE)
        db.session.commit()

        _reresolve_channel_url(rec.id, load_config())
        db.session.expire_all()

        self.assertEqual(db.session.get(Recording, rec.id).url, self.channel.stream_url)
        events = self._events(rec.id)
        self.assertEqual(len(events), 1)
        extra = json.loads(events[0].extra_data)
        self.assertEqual(extra['channel_id'], self.channel.id)
        self.assertNotIn('oldpass', extra['old_url'])
        self.assertNotIn('oldpass', events[0].detail)

    def test_matching_url_is_left_alone(self):
        rec = make_recording(channel_id=self.channel.id, url=self.channel.stream_url)
        db.session.commit()

        _reresolve_channel_url(rec.id, load_config())
        db.session.expire_all()

        self.assertEqual(db.session.get(Recording, rec.id).url, self.channel.stream_url)
        self.assertEqual(self._events(rec.id), [])

    def test_group_backed_recording_is_not_touched(self):
        """Group recordings resolve their own member at start and via failover; this
        helper must not race those writes."""
        group = make_group(members=[self.channel])
        rec = make_recording(channel_id=self.channel.id, group_id=group.id, url=STALE)
        db.session.commit()

        _reresolve_channel_url(rec.id, load_config())
        db.session.expire_all()

        self.assertEqual(db.session.get(Recording, rec.id).url, STALE)
        self.assertEqual(self._events(rec.id), [])

    def test_manual_url_only_recording_is_not_touched(self):
        rec = make_recording(channel_id=None, url=STALE)
        db.session.commit()

        _reresolve_channel_url(rec.id, load_config())
        db.session.expire_all()

        self.assertEqual(db.session.get(Recording, rec.id).url, STALE)
        self.assertEqual(self._events(rec.id), [])

    def test_ffmpeg_is_launched_against_the_fresh_url(self):
        """The invariant that actually matters: the URL handed to ffmpeg is the channel's
        current one, not the frozen one. _launch_segment bails right after spawning when the
        recording isn't in _active, so no watchdog thread starts."""
        rec = make_recording(status='IN_PROGRESS', channel_id=self.channel.id, url=STALE)
        db.session.commit()

        fake_proc = mock.MagicMock()
        fake_proc.pid = 4242
        fake_proc.poll.return_value = 0
        with mock.patch('app.recorder.subprocess.Popen', return_value=fake_proc) as popen:
            _launch_segment(self.t.app, rec.id, seg_num=1)

        cmd = popen.call_args[0][0]
        self.assertIn(self.channel.stream_url, cmd)
        self.assertNotIn(STALE, cmd)

    def test_resolved_url_respects_account_normalization(self):
        """The re-resolve goes through normalize_url, not a raw stream_url copy - otherwise a
        normalizing account's recordings would flip URL shape on their first relaunch."""
        acct = make_account(name='Normalizing', url_normalization=True)
        # Overridden rather than using the seed helper's default: normalization only acts on
        # URLs carrying a user/password/id triplet, and the shared fixture's
        # 'http://example.test/live/<id>' has none. That is deliberate - a real path with an
        # id and nothing else is an Icecast radio mount, not an Xtream stream, and rewriting
        # it would break it (changelog/258 Spec §2).
        channel = make_channel(acct, stream_id=77, name='Normalized Channel')
        channel.stream_url = 'http://example.test/live/AAA/BBB/77.ts'
        channel.raw_stream_url = 'http://example.test/live/AAA/BBB/77.ts'
        rec = make_recording(channel_id=channel.id, url=STALE)
        db.session.commit()

        _reresolve_channel_url(rec.id, load_config())
        db.session.expire_all()

        self.assertEqual(db.session.get(Recording, rec.id).url,
                         'http://example.test/AAA/BBB/77')


if __name__ == '__main__':
    unittest.main()
