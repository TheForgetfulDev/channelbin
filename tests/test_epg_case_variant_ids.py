"""Tier 2 - a file listing a channel under ids that differ only in case
(dev/docs/BUGS.md 2026-09-26 @ 06:15:57 PM, dev/changelog/1139).

Both provider guides list many channels twice - `ESPN.us` and `espn.us` - each with its own
schedule, and on account 5's guide the two are usually different feeds. With
case-insensitive matching the import read both into one channel, so it held two programs at
every start time. A channel now reads one id: the one spelled exactly as its key, else the
one with the most programs, ties to the lowest id. These tests pin:

  - ImportTests: one schedule per channel; each spelling reads its own variant; a spelling
    matching neither reads the larger; a tie resolves the same way on every import; the
    collapse guard's projected count equals the rows written; case-sensitive matching still
    reads only the exact id.
  - CopyTests: the paths that copy listings between channels instead of waiting for a
    refresh (setting a key, subscribing to another account's source, the "same listings"
    shortcut) copy the variant the next import would write, not whichever channel shares
    the folded key first.
  - ChannelDirectoryTests: the channel page's directory row is the variant the import reads.

No network - every feed is a local byte string (CLAUDE.md §Testing).
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import _count_projected_epg_entries, import_source  # noqa: E402
from app.database import Channel, EpgSource  # noqa: E402
from app.epg_sources import (KEY_COPIED, _channel_directory, set_channel_key,  # noqa: E402
                             subscribe_to_source)
from tests.support import seed  # noqa: E402
from tests.support.seed import make_epg_source  # noqa: E402
from tests.test_epg_sources import CFG, _Fixture, _schedule, _xmltv  # noqa: E402

UPPER = ['Upper News', 'Upper Film', 'Upper Sport']
LOWER = ['Lower News', 'Lower Film', 'Lower Sport']
LOWER_MORE = LOWER + ['Lower Late']


def _both(upper=UPPER, lower=LOWER):
    return _xmltv(_schedule('ESPN.us', upper) + _schedule('espn.us', lower))


class _VariantFixture(_Fixture):

    def _titles(self, ch):
        return [t for _s, t in self._active(ch)]


class ImportTests(_VariantFixture):

    def test_a_file_with_both_spellings_imports_one_schedule(self):
        ch = self._channel('ESPN', 'ESPN.us')
        synced, reason = import_source(self.src, _both(), epg_days=3, cfg=CFG)
        self.assertIsNone(reason)
        self.assertEqual(self._titles(ch), UPPER, 'one program per slot, not two')
        self.assertEqual(synced, 3)

    def test_each_spelling_reads_its_own_variant(self):
        upper = self._channel('US: ESPN UHD', 'ESPN.us')
        lower = self._channel('US: ESPN HD', 'espn.us')
        import_source(self.src, _both(), epg_days=3, cfg=CFG)
        self.assertEqual(self._titles(upper), UPPER)
        self.assertEqual(self._titles(lower), LOWER)

    def test_a_spelling_matching_neither_reads_the_one_with_more_programs(self):
        ch = self._channel('ESPN', 'Espn.US')
        import_source(self.src, _both(lower=LOWER_MORE), epg_days=3, cfg=CFG)
        self.assertEqual(self._titles(ch), LOWER_MORE)

    def test_a_tie_reads_the_same_variant_on_every_import(self):
        ch = self._channel('ESPN', 'Espn.US')
        for _ in range(2):
            import_source(self.src, _both(), epg_days=3, cfg=CFG)
            # 'ESPN.us' sorts before 'espn.us'.
            self.assertEqual(self._titles(ch), UPPER)

    def test_the_projected_count_is_the_rows_the_import_writes(self):
        now = datetime.utcnow()
        xml = _both(lower=LOWER_MORE)
        count = _count_projected_epg_entries(
            xml, {'ESPN.us': [1], 'espn.us': [2], 'Espn.US': [3, 4]}, False,
            now - timedelta(hours=1), now + timedelta(days=3))
        self.assertEqual(count.projected, 3 + 4 + 2 * 4)
        self.assertEqual(count.feed_map, {'ESPN.us': [1], 'espn.us': [2, 3, 4]})

        self._channel('A', 'ESPN.us')
        self._channel('B', 'espn.us')
        self._channel('C', 'Espn.US')
        synced, _reason = import_source(self.src, xml, epg_days=3, cfg=CFG)
        self.assertEqual(synced, 3 + 4 + 4)

    def test_case_sensitive_matching_reads_only_the_exact_id(self):
        exact = self._channel('ESPN', 'espn.us')
        neither = self._channel('ESPN 2', 'Espn.US')
        import_source(self.src, _both(), epg_days=3, case_sensitive=True, cfg=CFG)
        self.assertEqual(self._titles(exact), LOWER)
        self.assertEqual(self._titles(neither), [])


class CopyTests(_VariantFixture):
    """Alpha's source lists both spellings, and Alpha holds a channel on each."""

    def setUp(self):
        super().setUp()
        self.upper = self._channel('US: ESPN UHD', 'ESPN.us')
        self.lower = self._channel('US: ESPN HD', 'espn.us')
        import_source(self.src, _both(), epg_days=3, cfg=CFG)

    def _set(self, ch, key):
        change = set_channel_key(db.session.get(Channel, ch.id),
                                 db.session.get(EpgSource, self.src.id), key,
                                 case_sensitive=False)
        db.session.commit()
        db.session.expire_all()
        return change

    def test_setting_a_key_copies_the_variant_it_reads(self):
        ch = self._channel('Renamed ESPN', 'nothing.test')
        change = self._set(ch, 'espn.us')
        self.assertEqual(change.outcome, KEY_COPIED)
        self.assertIn(f'#{self.lower.id}', change.detail)
        self.assertEqual(self._titles(ch), LOWER)

    def test_respelling_a_key_is_not_the_same_listings_when_the_file_has_both(self):
        ch = self._channel('ESPN backup', 'ESPN.us')
        import_source(self.src, _both(), epg_days=3, cfg=CFG)
        self.assertEqual(self._titles(ch), UPPER)
        change = self._set(ch, 'espn.us')
        self.assertEqual(change.outcome, KEY_COPIED)
        self.assertEqual(self._titles(ch), LOWER)

    def test_subscribing_borrows_the_variant_each_channel_reads(self):
        beta = seed.make_account(name='Beta')
        make_epg_source(beta)
        b_lower = seed.make_channel(beta, name='Beta ESPN', epg_channel_id='espn.us')
        b_upper = seed.make_channel(beta, name='Beta ESPN UHD', epg_channel_id='ESPN.us')
        db.session.commit()
        subscribe_to_source(beta.id, self.src.id, case_sensitive=False)
        db.session.expire_all()
        self.assertEqual(self._titles(b_lower), LOWER)
        self.assertEqual(self._titles(b_upper), UPPER)

        # And the owner's next import writes exactly what the borrow did.
        import_source(db.session.get(EpgSource, self.src.id), _both(), epg_days=3, cfg=CFG)
        self.assertEqual(self._titles(b_lower), LOWER)


class ChannelDirectoryTests(_VariantFixture):

    def test_the_directory_row_is_the_variant_the_import_reads(self):
        ch = self._channel('ESPN', 'espn.us')
        import_source(self.src, _both(upper=UPPER + ['Upper Late']), epg_days=3, cfg=CFG)
        _keys, _wanted, directory = _channel_directory(
            db.session.get(Channel, ch.id), [self.src.id], False)
        self.assertEqual(directory[self.src.id].xml_id, 'espn.us',
                         'its own spelling, though the other variant lists more programs')
