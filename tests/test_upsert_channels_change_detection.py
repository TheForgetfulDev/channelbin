"""`_upsert_channels` only UPDATEs channels the provider actually changed.

Guards dev/docs/BUGS.md 2026-08-15 07:08 PM. The update branch used to assign every
provider field plus `updated_at`/`last_seen_at` unconditionally on every matched row, so
a sync whose feed came back byte-identical still dirtied the whole account and flushed one
UPDATE per channel - 57,170 statements on the largest real account here, six times a day,
all inside the single write transaction the sync's commit closure holds. Two separate
defects came out of that: the write-lock hold (measured 10.74s -> 1.14s for that account,
dev/changelog/673), and `updated_at` claiming every channel was edited on every sync,
which made it useless as a "did this row change" signal for anything downstream.

Covers:
  - ChangeDetectionTests: an identical re-sync dirties nothing and moves no updated_at,
    while still advancing last_seen_at; each provider field, changed on its own, is
    detected and does move updated_at; the fallback semantics (provider omits or empties
    a field) still read as "no change".
  - LastSeenStampFidelityTests: the two mechanics of the bulk stamp that are silent
    corruption if reversed - the suppressed `onupdate`, and microsecond precision.
  - StatementScalingTests: UPDATE count does not scale with channel count.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_upsert_channels_change_detection
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

from sqlalchemy import event

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import _upsert_channels  # noqa: E402
from app.database import Channel, M3uAccount  # noqa: E402
from tests.support import make_test_app  # noqa: E402

M3U_URL = 'http://provider.test/playlist.m3u8?user=realuser&pass=realpass'


def _stream(sid, **overrides):
    """One M3U-sourced stream dict. `_stream_url` is present, so no URL construction."""
    base = {
        'stream_id': sid,
        'name': f'Ch{sid}',
        '_stream_url': f'http://provider.test/live/u/p/{sid}.ts',
        'stream_icon': f'http://provider.test/logo{sid}.png',
        'category_id': '7',
        'category_name': 'Sports',
        'epg_channel_id': f'ch{sid}.test',
    }
    base.update(overrides)
    return base


class _UpsertCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Change Detection', m3u_url=M3U_URL, status='OK')
        db.session.add(self.account)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _sync(self, streams):
        result = _upsert_channels(self.account, streams)
        db.session.commit()
        db.session.expire_all()
        return result

    def _row(self, sid=1):
        return Channel.query.filter_by(account_id=self.account.id, stream_id=sid).first()

    def _count_updates(self, streams):
        """UPDATE statements emitted against `channels` by one _upsert_channels call.

        Counts ROWS addressed, not calls: SQLAlchemy batches per-row updates into one
        executemany, which would otherwise read as a single cheap statement. The bulk
        last_seen_at stamp addresses many rows in one statement by design, so it counts
        as the 1 it is - that difference is exactly what these tests are measuring.
        """
        updates = []
        engine = db.session.get_bind()

        def _before(conn, cursor, statement, params, context, executemany):
            if statement.lstrip().upper().startswith('UPDATE CHANNELS'):
                updates.append(len(params) if executemany else 1)

        event.listen(engine, 'before_cursor_execute', _before)
        try:
            _upsert_channels(self.account, streams)
            db.session.flush()
        finally:
            event.remove(engine, 'before_cursor_execute', _before)
        db.session.commit()
        db.session.expire_all()
        return sum(updates)


class ChangeDetectionTests(_UpsertCase):
    def test_identical_resync_does_not_move_updated_at(self):
        """The signal item #12's search-index watermark depends on: a sync that changed
        nothing must leave updated_at exactly where it was."""
        self._sync([_stream(1), _stream(2), _stream(3)])
        before = {c.stream_id: c.updated_at for c in Channel.query.all()}

        self._sync([_stream(1), _stream(2), _stream(3)])

        after = {c.stream_id: c.updated_at for c in Channel.query.all()}
        self.assertEqual(before, after,
                         'an identical re-sync moved updated_at - every channel still '
                         'claims it was edited by a sync that changed nothing')

    def test_identical_resync_still_advances_last_seen_at(self):
        """Change detection must not cost the lifecycle contract: last_seen_at means
        "this sync's feed contained the channel", not "this sync changed it"."""
        self._sync([_stream(1), _stream(2)])
        stale = datetime.utcnow() - timedelta(days=5)
        for ch in Channel.query.all():
            ch.last_seen_at = stale
        db.session.commit()

        self._sync([_stream(1), _stream(2)])

        for ch in Channel.query.all():
            self.assertGreater(ch.last_seen_at, stale,
                               f'stream {ch.stream_id} was in the feed but was not stamped')

    def test_unchanged_resync_emits_one_bulk_stamp_not_one_update_per_channel(self):
        """The whole point: three unchanged channels cost one statement, not three."""
        self._sync([_stream(1), _stream(2), _stream(3)])
        self.assertEqual(self._count_updates([_stream(1), _stream(2), _stream(3)]), 1,
                         'unchanged channels were UPDATEd individually')

    def test_each_provider_field_is_detected_on_its_own(self):
        """One case per field the update branch writes. A field missing from this
        comparison is a channel that silently stops tracking its provider.

        Each field gets its own stream_id so the cases share one app - a channel absent
        from a later feed is simply left untouched, which is the documented no-delete
        behavior (DESIGN-sync-resilience.md §1).
        """
        cases = [
            ('name', {'name': 'Renamed'}, lambda c: c.name == 'Renamed'),
            ('logo_url', {'stream_icon': 'http://provider.test/new.png'},
             lambda c: c.logo_url.endswith('new.png')),
            ('category_name', {'category_name': 'Movies'},
             lambda c: c.category_name == 'Movies'),
            ('category_id', {'category_id': '99'}, lambda c: c.category_id == '99'),
            ('epg_channel_id', {'epg_channel_id': 'moved.test'},
             lambda c: c.epg_channel_id == 'moved.test'),
            ('raw_stream_url', {'_stream_url': 'http://provider.test/live/u/p/999.ts'},
             lambda c: c.raw_stream_url.endswith('999.ts')),
        ]
        for i, (field, override, check) in enumerate(cases, start=100):
            with self.subTest(field=field):
                self._sync([_stream(i)])
                original = self._row(i).updated_at

                self._sync([_stream(i, **override)])

                row = self._row(i)
                self.assertTrue(check(row), f'{field} was not written to the row')
                self.assertGreater(
                    row.updated_at, original,
                    f'{field} changed but updated_at did not move - the row was '
                    'treated as unchanged')

    def test_omitted_fallback_field_keeps_stored_value_and_reads_as_unchanged(self):
        """The four fallback fields are load-bearing: a provider that stops sending
        name/logo/category keeps the stored value, and that must not register as a
        change. (The other four have no fallback and never did - an omitted
        epg_channel_id genuinely clears the stored one, asserted separately below.)"""
        self._sync([_stream(1)])
        before = self._row().updated_at

        self._sync([{'stream_id': 1, 'epg_channel_id': 'ch1.test',
                     '_stream_url': 'http://provider.test/live/u/p/1.ts'}])

        row = self._row()
        self.assertEqual(row.name, 'Ch1', 'an omitted name overwrote the stored one')
        self.assertTrue(row.logo_url.endswith('logo1.png'))
        self.assertEqual(row.category_name, 'Sports')
        self.assertEqual(row.category_id, '7')
        self.assertEqual(row.updated_at, before,
                         'a provider omitting fallback fields was treated as a change')

    def test_omitted_epg_channel_id_still_clears_the_stored_one(self):
        """Pins the pre-existing asymmetry so change detection can never be "fixed" into
        giving epg_channel_id a fallback it never had."""
        self._sync([_stream(1)])
        before = self._row().updated_at

        self._sync([{'stream_id': 1,
                     '_stream_url': 'http://provider.test/live/u/p/1.ts'}])

        row = self._row()
        self.assertEqual(row.epg_channel_id, '')
        self.assertGreater(row.updated_at, before,
                           'clearing epg_channel_id is a real change and must move '
                           'updated_at')

    def test_empty_field_keeps_stored_value_and_reads_as_unchanged(self):
        self._sync([_stream(1)])
        before = self._row().updated_at

        self._sync([_stream(1, stream_icon='', category_name='', category_id='')])

        row = self._row()
        self.assertTrue(row.logo_url.endswith('logo1.png'))
        self.assertEqual(row.category_name, 'Sports')
        self.assertEqual(row.updated_at, before,
                         'empty provider fields were treated as a change')

    def test_changed_and_unchanged_rows_in_one_sync(self):
        self._sync([_stream(1), _stream(2), _stream(3)])
        untouched = {sid: self._row(sid).updated_at for sid in (1, 3)}

        self._sync([_stream(1), _stream(2, name='Only This One'), _stream(3)])

        self.assertEqual(self._row(2).name, 'Only This One')
        for sid, was in untouched.items():
            self.assertEqual(self._row(sid).updated_at, was,
                             f'stream {sid} was dirtied by a sibling row changing')

    def test_new_channels_are_unaffected(self):
        """The insert branch is untouched - new rows still get both timestamps and are
        still reported back as new."""
        self._sync([_stream(1)])
        _, _, _, _, new_ids = self._sync([_stream(1), _stream(2)])

        self.assertEqual(len(new_ids), 1)
        added = self._row(2)
        self.assertEqual(new_ids, [added.id])
        self.assertIsNotNone(added.first_seen_at)
        self.assertIsNotNone(added.last_seen_at)


class LastSeenStampFidelityTests(_UpsertCase):
    """The two properties of the bulk last_seen_at stamp that are silent corruption if the
    statement is ever "simplified" - see app/accounts.py::_stamp_last_seen."""

    def test_bulk_stamp_suppresses_the_onupdate_default(self):
        """A Core UPDATE fires Channel.updated_at's own onupdate=utcnow unless the column
        is named in the SET clause, which would restore the exact defect this fixes."""
        self._sync([_stream(1)])
        before = self._row().updated_at

        self._sync([_stream(1)])

        self.assertEqual(self._row().updated_at, before,
                         "the bulk stamp fired updated_at's onupdate default")

    def test_stamp_keeps_microsecond_precision(self):
        """last_seen_at is compared with `<` against the sync's own timestamp in
        _raise_channel_lifecycle_alerts. A stamp truncated to whole seconds can sort BELOW
        the sync that just wrote it, reporting every channel as absent from its own feed
        (a spurious SYNC_FEED_SHRUNK alert)."""
        self._sync([_stream(1), _stream(2)])
        # Force a non-zero microsecond component on the stored value, then re-sync so the
        # bulk stamp is what writes it.
        self._sync([_stream(1), _stream(2)])

        stamped = [c.last_seen_at for c in Channel.query.all()]
        self.assertTrue(
            any(ts.microsecond for ts in stamped),
            'every last_seen_at landed on a whole second - the stamp is being stored '
            'through a path that drops microseconds')

    def test_stamp_never_reads_as_older_than_the_sync_that_wrote_it(self):
        """The consequence the precision test protects against, asserted directly:
        after a sync, no channel it matched may look absent from that sync's feed."""
        self._sync([_stream(1), _stream(2), _stream(3)])
        for _ in range(3):
            sync_start = datetime.utcnow()
            self._sync([_stream(1), _stream(2), _stream(3)])
            for ch in Channel.query.all():
                self.assertGreaterEqual(
                    ch.last_seen_at, sync_start,
                    f'stream {ch.stream_id} was in the feed but its last_seen_at sorts '
                    'before the sync that stamped it')


class StatementScalingTests(_UpsertCase):
    """CLAUDE.md's no-hidden-work rule applied to the write side: an unchanged re-sync
    must cost the same number of UPDATEs at 3 channels as at 200."""

    def test_update_count_does_not_scale_with_channel_count(self):
        few = [_stream(i) for i in range(1, 4)]
        self._sync(few)
        few_updates = self._count_updates(few)

        many = [_stream(i) for i in range(1000, 1200)]
        self._sync(many)
        many_updates = self._count_updates(many)

        self.assertEqual(
            few_updates, many_updates,
            f'UPDATE count scales with channel count ({few_updates} at 3 channels vs '
            f'{many_updates} at 200) - unchanged rows are still being dirtied')
        self.assertEqual(many_updates, 1, 'expected exactly one bulk last_seen_at stamp')


if __name__ == '__main__':
    unittest.main()
