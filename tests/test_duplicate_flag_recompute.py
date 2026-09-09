"""app/accounts.py::_recompute_duplicate_stream_urls - the cross-account duplicate-URL flag.

The flag drives what channel search hides by default and is the cheap prefilter the
missing-channel re-point helper keys off, so which rows end up flagged is a product-visible
answer. dev/changelog/684 replaced the function's full-table ORM scan with two Core UPDATEs;
these tests pin both halves of that:

  - RecomputeResultTests: the answer itself, one rule at a time. These are characterization
    tests - they pass against the pre-684 implementation too, deliberately, because the whole
    claim of that change is that the answer did not move.
  - RecomputeStatementShapeTests: the change that IS the point - the recompute must not
    hydrate channel rows into the session, and must write through bounded SQL rather than a
    per-row ORM flush. These fail against the pre-684 implementation.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_duplicate_flag_recompute
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.database import Channel  # noqa: E402
from app.accounts import _recompute_duplicate_stream_urls  # noqa: E402
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.iocount import IOCounter, all_engines  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.account_a = seed.make_account(name='Acct A')
        self.account_b = seed.make_account(name='Acct B')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _channel(self, account, stream_id, url, **kw):
        ch = seed.make_channel(account, stream_id=stream_id, name=f'Ch {stream_id}', **kw)
        ch.stream_url = url
        return ch

    def _flagged(self):
        """The flag as the DATABASE holds it, not as the session remembers it - the Core
        UPDATE writes past the identity map on purpose (synchronize_session=False)."""
        db.session.expire_all()
        return {ch.id for ch in Channel.query.filter(
            Channel.is_duplicate_stream_url.is_(True)).all()}


class RecomputeResultTests(_Base):
    def test_url_shared_across_two_accounts_flags_both(self):
        """The whole reason this runs after every sync of any account: a duplicate can be
        created by a channel in a DIFFERENT account than the one that just synced."""
        a = self._channel(self.account_a, 1, 'http://example.test/live/shared')
        b = self._channel(self.account_b, 1, 'http://example.test/live/shared')
        db.session.commit()

        _recompute_duplicate_stream_urls()
        db.session.commit()
        self.assertEqual(self._flagged(), {a.id, b.id})

    def test_unique_url_is_not_flagged(self):
        a = self._channel(self.account_a, 1, 'http://example.test/live/1')
        self._channel(self.account_a, 2, 'http://example.test/live/2')
        db.session.commit()

        _recompute_duplicate_stream_urls()
        db.session.commit()
        self.assertEqual(self._flagged(), set())
        self.assertFalse(db.session.get(Channel, a.id).is_duplicate_stream_url)

    def test_every_member_of_a_three_way_group_is_flagged(self):
        ids = [self._channel(self.account_a, i, 'http://example.test/live/shared').id
               for i in (1, 2, 3)]
        db.session.commit()

        _recompute_duplicate_stream_urls()
        db.session.commit()
        self.assertEqual(self._flagged(), set(ids))

    def test_malformed_url_shared_by_two_channels_is_not_a_duplicate(self):
        """A URL with no '://' is provider placeholder noise (the literal string "http"),
        not a real stream, so two channels carrying it are not duplicates of each other -
        it would otherwise flag every placeholder row in the catalog as a duplicate."""
        self._channel(self.account_a, 1, 'http')
        self._channel(self.account_b, 1, 'http')
        db.session.commit()

        _recompute_duplicate_stream_urls()
        db.session.commit()
        self.assertEqual(self._flagged(), set())

    def test_stale_true_flag_is_cleared_when_the_duplicate_goes_away(self):
        """The other direction: the flag is recomputed, not accumulated."""
        survivor = self._channel(self.account_a, 1, 'http://example.test/live/shared',
                                 is_duplicate_stream_url=True)
        gone = self._channel(self.account_b, 1, 'http://example.test/live/shared',
                             is_duplicate_stream_url=True)
        db.session.commit()
        db.session.delete(gone)
        db.session.commit()

        _recompute_duplicate_stream_urls()
        db.session.commit()
        self.assertEqual(self._flagged(), set())
        self.assertFalse(db.session.get(Channel, survivor.id).is_duplicate_stream_url)

    def test_stale_true_flag_on_a_malformed_url_is_cleared(self):
        """The skip rule has to reach the clearing direction too, or a row flagged before
        it existed stays flagged forever."""
        ch = self._channel(self.account_a, 1, 'http', is_duplicate_stream_url=True)
        db.session.commit()

        _recompute_duplicate_stream_urls()
        db.session.commit()
        self.assertFalse(db.session.get(Channel, ch.id).is_duplicate_stream_url)

    def test_already_correct_flags_are_left_alone(self):
        dup_a = self._channel(self.account_a, 1, 'http://example.test/live/shared',
                              is_duplicate_stream_url=True)
        dup_b = self._channel(self.account_b, 1, 'http://example.test/live/shared',
                              is_duplicate_stream_url=True)
        unique = self._channel(self.account_a, 2, 'http://example.test/live/2',
                               is_duplicate_stream_url=False)
        db.session.commit()

        _recompute_duplicate_stream_urls()
        db.session.commit()
        self.assertEqual(self._flagged(), {dup_a.id, dup_b.id})
        self.assertFalse(db.session.get(Channel, unique.id).is_duplicate_stream_url)

    def test_no_channels_at_all_is_not_an_error(self):
        _recompute_duplicate_stream_urls()
        db.session.commit()
        self.assertEqual(self._flagged(), set())

    def test_pending_channels_are_included(self):
        """The sync calls this with account-row updates still pending in the session, so
        the statements have to autoflush rather than read a pre-flush snapshot."""
        a = self._channel(self.account_a, 1, 'http://example.test/live/shared')
        b = self._channel(self.account_b, 1, 'http://example.test/live/shared')
        db.session.commit()
        c = Channel(account_id=self.account_a.id, stream_id=99, name='Pending',
                    stream_url='http://example.test/live/shared')
        db.session.add(c)  # deliberately NOT flushed

        _recompute_duplicate_stream_urls()
        db.session.commit()
        self.assertEqual(self._flagged(), {a.id, b.id, c.id})


class RecomputeStatementShapeTests(_Base):
    """dev/changelog/684 - the recompute used to hydrate every Channel row in the database (138,415 on
    the real one, 5.80s warm to change nothing) to flip a boolean. It must now cost a bounded
    number of statements and materialize no ORM instances, whatever the table's size."""

    def _seed(self, n_dup_pairs=3, n_unique=4, n_stale_flags=2):
        stream_id = 0
        for i in range(n_dup_pairs):
            for account in (self.account_a, self.account_b):
                stream_id += 1
                self._channel(account, stream_id, f'http://example.test/live/dup{i}')
        for i in range(n_unique):
            stream_id += 1
            self._channel(self.account_a, stream_id, f'http://example.test/live/uniq{i}')
        for i in range(n_stale_flags):
            stream_id += 1
            self._channel(self.account_a, stream_id, f'http://example.test/live/stale{i}',
                          is_duplicate_stream_url=True)
        db.session.commit()

    def test_recompute_hydrates_no_channel_rows(self):
        """No statement may select a channel column the duplicate decision does not need.
        `Channel.query.all()` selected all ~30 of them AND dragged the lazy='joined'
        recording_profiles LEFT JOIN along for the ride."""
        self._seed()
        with IOCounter(all_engines()) as c:
            _recompute_duplicate_stream_urls()
        joined = ' | '.join(' '.join(s.split()) for s in c.statements)
        self.assertNotIn('channels.name', joined)
        self.assertNotIn('recording_profiles', joined)

    def test_recompute_is_two_statements_regardless_of_how_many_rows_flip(self):
        """Both directions of the flip in one pass each - not one UPDATE per changed row,
        and not a count that grows with the table."""
        self._seed(n_dup_pairs=6, n_unique=8, n_stale_flags=4)
        with IOCounter(all_engines()) as c:
            _recompute_duplicate_stream_urls()
        self.assertEqual(c.queries, 2, c.statements)
        self.assertTrue(all(s.strip().upper().startswith('UPDATE') for s in c.statements),
                        c.statements)

    def test_recompute_leaves_nothing_dirty_in_the_session(self):
        """The flip lands in the transaction, not in pending ORM state - which is what lets
        the caller's own commit be the only thing left to retry."""
        self._seed()
        _recompute_duplicate_stream_urls()
        self.assertEqual(list(db.session.dirty), [])


if __name__ == '__main__':
    unittest.main()
