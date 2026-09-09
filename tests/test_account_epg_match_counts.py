"""Regression: the account page's per-account EPG-match diagnostic
(app/routes/accounts.py::_epg_match_counts) must compute its three stats with
aggregate SQL, never by hydrating every Channel row for the account.

Originally guarded the now-retired /guide/epg (EPG Browser) page (BUGS.md 2026-07-18:
~3.2s on the live 36k-channel DB because the view ran
`Channel.query.filter_by(account_id=acc.id).all()` per account - loading every Channel
ORM instance just to count four numbers - and scanned the 1.3M-row epg_entries table on
the default page). The diagnostic moved onto the account detail page's Content card
when that page was retired (dev/changelog/631); the invariant it guards is unchanged,
just at a new call site. Same defect class as test_scaling.py: work that must not scale
with row count.

Two invariants:
  1. Scaling - loading the account page must load a number of Channel ORM instances
     that does NOT grow with the account's channel count. It loads zero today; the test
     only requires row-count independence so a future refactor that legitimately loads
     a small constant number still passes.
  2. Correctness - the three rendered stat numbers (with EPG match / ID-set-no-match /
     no EPG ID) must match a brute-force count over the seeded channels.
"""
import os
import re
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event  # noqa: E402

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import Channel  # noqa: E402


class _ChannelLoadCounter:
    """Count how many Channel ORM instances are loaded from the DB (the ORM 'load'
    event fires once per row hydrated into a Channel object)."""

    def __init__(self):
        self.count = 0

    def _on_load(self, target, context):
        self.count += 1

    def __enter__(self):
        event.listen(Channel, 'load', self._on_load)
        return self

    def __exit__(self, *exc):
        event.remove(Channel, 'load', self._on_load)
        return False


def _seed_account_with_channels(n_channels):
    """One account with n_channels, each with an EPG id and one future program (so
    every channel counts as 'with EPG'). Committed. Returns the account."""
    acc = seed.make_account()
    base = datetime.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(n_channels):
        ch = seed.make_channel(acc)
        db.session.add(seed.EPGEntry(
            channel_id=ch.id, title='Show',
            start_time=base, stop_time=base + timedelta(hours=1)))
    db.session.commit()
    return acc


def _stat(html, label):
    """Extract the numeric stat rendered in the account page's Content card for the
    given label (`<span class="sk">label</span><span class="sv...">N</span>`, N
    sometimes wrapped in a jump-off `<a>` like the Channels row)."""
    m = re.search(r'<span class="sk">' + re.escape(label) + r'</span>'
                  r'<span class="sv[^"]*"[^>]*>\s*(?:<a[^>]*>)?([\d,]+)', html)
    assert m, f'stat labelled {label!r} not found in rendered page'
    return int(m.group(1).replace(',', ''))


class AccountEpgMatchCountsTests(unittest.TestCase):
    def _channel_loads_for(self, n_channels):
        t = make_test_app()
        try:
            acc = _seed_account_with_channels(n_channels)
            with _ChannelLoadCounter() as counter:
                resp = t.client.get(f'/accounts/{acc.id}')
            self.assertEqual(resp.status_code, 200,
                             f'/accounts/{acc.id} returned {resp.status_code}, not 200')
            return counter.count
        finally:
            t.cleanup()

    def test_default_page_channel_loads_do_not_scale(self):
        small = self._channel_loads_for(5)
        large = self._channel_loads_for(50)
        self.assertEqual(
            small, large,
            f'The account page loads a number of Channel ORM instances that scales '
            f'with the account channel count ({small} at 5 channels vs {large} at 50) - '
            f'the EPG-match stats are hydrating every Channel row instead of using '
            f'aggregate SQL (BUGS.md 2026-07-18).')

    def test_stats_are_correct(self):
        t = make_test_app()
        try:
            acc = seed.make_account()
            base = datetime.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            # 2 with EPG (id set + future program)
            for _ in range(2):
                ch = seed.make_channel(acc)
                db.session.add(seed.EPGEntry(
                    channel_id=ch.id, title='Show',
                    start_time=base, stop_time=base + timedelta(hours=1)))
            # 1 id set, no programs -> "EPG id set, no match"
            seed.make_channel(acc)
            # 1 with no EPG id at all -> "No EPG id" (make_channel always sets one)
            seed.make_channel(acc).epg_channel_id = ''
            # channel_count is a cache column refreshed by a real sync (BUGS.md-unrelated
            # to the EPG-match diagnostic, which counts Channel rows live) - set it to
            # match so the "Channels" row is a meaningful cross-check, not a false 0.
            acc.channel_count = 4
            db.session.commit()

            resp = t.client.get(f'/accounts/{acc.id}')
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            self.assertEqual(_stat(html, 'Channels'), 4)
            self.assertEqual(_stat(html, 'Channels with EPG match'), 2)
            self.assertEqual(_stat(html, 'EPG id set, no match'), 1)
            self.assertEqual(_stat(html, 'No EPG id'), 1)
        finally:
            t.cleanup()


if __name__ == '__main__':
    unittest.main(verbosity=2)
