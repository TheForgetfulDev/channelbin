"""The daily EPG retention sweep must keep Account.epg_entry_count accurate.

Pins BUGS.md 2026-07-23 (changelog/267). `cleanup_old_epg_entries()` bulk-deletes expired
EPGEntry rows once a day but, before this fix, never recomputed the stored
`Account.epg_entry_count` - so the count drifted high after every sweep and only self-healed
at the account's next full sync (confirmed live on Account 3: 303,797 stored vs.
191,891 actual). The fix recomputes each account's count in the same transaction, right after
the delete, before commit.

Two invariants:
  1. After a sweep that deletes rows, `Account.epg_entry_count` equals the actual remaining
     EPGEntry count for that account (not the pre-sweep stored value).
  2. A sweep that deletes nothing leaves the stored count untouched (the `if n:` guard - no
     gratuitous recompute).

`load_config` is patched at `app.accounts.load_config` (a module-top binding) so the sweep
sees a fixed `sync.epg_keep_days`; make_test_app overrides are not visible to a runtime
load_config() (CLAUDE.md §Testing).

No network, throwaway temp SQLite DB - never the live dvr.db.
Run standalone:
  python3 -m unittest tests.test_epg_retention_count
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import cleanup_old_epg_entries  # noqa: E402
from app.database import EPGEntry  # noqa: E402
from tests.support import make_test_app, seed  # noqa: E402

# stop_time this far in the past is well before a 1-day cutoff.
TEN_DAYS_MIN = 10 * 24 * 60


class EpgRetentionCountTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.account = seed.make_account(name='Retention Acct')
        self.channel = seed.make_channel(self.account, name='Ch 1')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _run_sweep(self, keep_days=1):
        fake_cfg = {'sync': {'epg_keep_days': keep_days}}
        with mock.patch('app.accounts.load_config', return_value=fake_cfg):
            cleanup_old_epg_entries(self.t.app)
        # cleanup commits inside its own app_context() (separate session).
        db.session.expire_all()

    def test_count_recomputed_after_delete(self):
        # One expired entry (deleted by the sweep) + one fresh entry (survives).
        seed.make_epg_entry(self.channel, title='Old', offset_minutes=-TEN_DAYS_MIN)
        seed.make_epg_entry(self.channel, title='Fresh', offset_minutes=0)
        self.account.epg_entry_count = 999  # deliberately stale
        db.session.commit()

        self._run_sweep(keep_days=1)

        remaining = EPGEntry.query.filter_by(channel_id=self.channel.id).count()
        self.assertEqual(remaining, 1)
        self.assertEqual(self.account.epg_entry_count, 1)

    def test_count_untouched_when_nothing_deleted(self):
        # Only a fresh entry; the sweep deletes nothing, so the stored count is left as-is
        # (the `if n:` guard - no gratuitous recompute).
        seed.make_epg_entry(self.channel, title='Fresh', offset_minutes=0)
        self.account.epg_entry_count = 999  # stale, but must not be recomputed here
        db.session.commit()

        self._run_sweep(keep_days=1)

        self.assertEqual(self.account.epg_entry_count, 999)


if __name__ == '__main__':
    unittest.main()
