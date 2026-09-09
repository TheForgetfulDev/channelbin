"""Tier 2 - URL re-normalization propagates without an account sync (dev/changelog/308,
"Verify: re-normalizing URLs without a sync propagates everywhere the URL is used").

app/recorder.py::_reresolve_channel_url already proves the recording-launch half
(tests/test_url_drift.py). This file covers the two remaining consumers named in that
spec: health checks (app/channel_tester.py) and a channel's group-member selection
(app/recorder.py::start_recording), neither of which freezes a URL onto a row - both
must reflect a Channel.stream_url / account normalization-mode change on their very next
use, with no sync and no explicit re-resolve call in between.

Guards:
  * a health check connects ffmpeg to the channel's CURRENTLY-normalized URL, not the
    stale spelling that happened to be stored on the row (app/channel_tester.py did not
    call normalize_url() at all before this fix - it used the raw column value);
  * the same normalized URL is what the live status/log surfaces show;
  * a group-backed recording's member-selection resolves the picked member's URL through
    the account's current normalization mode too (already-covered call site, asserted
    here for the "everywhere" half of the item).

Runs against a throwaway temp SQLite DB - never the live dvr.db. No real ffmpeg or
network: subprocess.Popen and the connect-wait are monkeypatched.
  python3 -m unittest tests.test_url_propagation
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

from app import db, channel_tester, connection_limits as connlim, recorder  # noqa: E402
from app.accounts import NORM_HLS  # noqa: E402
from app.database import ChannelTest  # noqa: E402

# Stored in the "mpegts" spelling - a real channel synced under that mode.
STORED_MPEGTS = 'http://example.test/AAA/BBB/55'
# What the SAME channel/creds/id look like once re-normalized under "hls".
EXPECTED_HLS = 'http://example.test/live/AAA/BBB/55.m3u8'


class FakeProc:
    """No-data ffmpeg stand-in - just enough for the tester's connect loop to fail
    cleanly on the first attempt without waiting on anything real."""
    def __init__(self):
        self._returncode = 1

    def poll(self):
        return self._returncode

    def terminate(self):
        self._returncode = -15

    def kill(self):
        self._returncode = -9

    def wait(self, timeout=None):
        return self._returncode

    stderr = None


class HealthCheckNormalizationTests(unittest.TestCase):
    """A channel's stream_url was stored under one normalization mode; the account's
    mode setting changes afterward with NO resync. The next health check must connect
    (and report) using the NEW mode, not the mode that was active when the row was
    written."""

    def setUp(self):
        self.t = make_test_app()
        with self.t.app.app_context():
            # Mode set to hls at account-creation time, but the channel row below was
            # written as if synced earlier under mpegts - exactly what "changed the
            # setting, didn't resync yet" leaves on disk.
            acct = seed.make_account(name='Renorm Acct', url_normalization=NORM_HLS)
            ch = seed.make_channel(acct, name='Renorm Channel')
            ch.stream_url = STORED_MPEGTS
            ch.raw_stream_url = STORED_MPEGTS
            db.session.commit()
            self.channel_id = ch.id
        # No _reset_run_state() here: make_test_app() has already swapped in a fresh
        # RunState via reset_module_globals(), and _reset_run_state() is a start-of-run
        # function that takes the KIND_TESTER admission ticket (dev/changelog/723).
        connlim._holders.clear()

    def tearDown(self):
        # _end_run(), not _reset_run_state(): a test that drove a run to completion may
        # still hold the ticket, and this is the one path that gives it back.
        channel_tester._end_run()
        connlim._holders.clear()
        self.t.cleanup()

    def _run(self):
        procs = []

        def _popen(cmd, **kw):
            procs.append(cmd)
            return FakeProc()

        with mock.patch.object(channel_tester.subprocess, 'Popen', _popen), \
             mock.patch.object(channel_tester, '_drain_stderr', lambda *a, **kw: None), \
             mock.patch.object(channel_tester, 'wait_for_file_data', lambda *a, **kw: False), \
             mock.patch.object(channel_tester, '_interruptible_sleep', lambda *a, **kw: None):
            channel_tester.run_channel_test(self.t.app, self.channel_id)
        return procs

    def test_ffmpeg_connects_to_the_renormalized_url(self):
        procs = self._run()
        self.assertGreaterEqual(len(procs), 1)
        cmd = procs[0]
        self.assertIn(EXPECTED_HLS, cmd)
        self.assertNotIn(STORED_MPEGTS, cmd)

    def test_live_status_reports_the_renormalized_url(self):
        self._run()
        status = channel_tester.get_status()
        # mask_creds masks the user/pass segment; assert on the parts it leaves alone
        # (scheme+host+path shape) rather than the exact masked string.
        self.assertIn('/live/', status['current_channel_url'])
        self.assertTrue(status['current_channel_url'].endswith('.m3u8'))

    def test_error_log_lines_cite_the_renormalized_url_masked(self):
        self._run()
        with self.t.app.app_context():
            db.session.expire_all()
            row = (ChannelTest.query.filter_by(channel_id=self.channel_id)
                   .order_by(ChannelTest.id.desc()).first())
        logged = ' '.join(e['msg'] for e in channel_tester._state.log_entries)
        self.assertIn('.m3u8', logged)
        self.assertNotIn('AAA', logged, 'credentials must be masked in the log')
        self.assertIsNotNone(row)


class GroupMemberSelectionNormalizationTests(unittest.TestCase):
    """A group-backed recording resolves its member's URL fresh at start_recording -
    prove that resolution goes through the account's CURRENT normalization mode too,
    not a raw copy of Channel.stream_url."""

    def setUp(self):
        self.t = make_test_app()
        with self.t.app.app_context():
            acct = seed.make_account(name='Group Renorm Acct', url_normalization=NORM_HLS)
            ch = seed.make_channel(acct, name='Group Member')
            ch.stream_url = STORED_MPEGTS
            ch.raw_stream_url = STORED_MPEGTS
            group = seed.make_group(name='Renorm Group', members=[ch])
            db.session.commit()
            rec = seed.make_recording(status='SCHEDULED', channel_id=None, group_id=group.id)
            db.session.commit()
            self.recording_id = rec.id

    def tearDown(self):
        self.t.cleanup()

    def test_group_member_url_is_resolved_under_the_current_mode(self):
        with mock.patch.object(recorder, '_try_acquire_slot_with_preemption'), \
             mock.patch.object(recorder, '_launch_segment'):
            recorder.start_recording(self.t.app, self.recording_id)

        with self.t.app.app_context():
            db.session.expire_all()
            from app.database import Recording
            rec = db.session.get(Recording, self.recording_id)
            self.assertEqual(rec.url, EXPECTED_HLS)


if __name__ == '__main__':
    unittest.main()
