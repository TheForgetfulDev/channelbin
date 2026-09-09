"""A lock retry on the sync's success mark must not redo its reads or its recompute.

Guards dev/docs/BUGS.md 2026-08-16 @ 12:44:58 PM ET. `_mark_success_and_commit` assigned the
account row first and only then ran two COUNTs and the cross-account duplicate-URL recompute,
so the first of those queries autoflushed the pending account UPDATE, took SQLite's single
write lock, and the rest of the read pass ran while holding it - and because it was all one
`retry_on_locked` closure, one "database is locked" redid both counts and the recompute.

The injection is the real mechanism, borrowed from tests/test_sync_retry_boundaries.py:
`retry_on_locked` is replaced with one that fails the success closure's FIRST commit with the
exact OperationalError SQLite raises, rolls the session back the way the real decorator does,
and re-runs the closure. Whatever is inside that closure therefore runs twice, which is the
property under test.

No network: the M3U playlist is served by a patched `requests.get` (CLAUDE.md §Testing).

Run standalone:
  python3 -m unittest tests.test_sync_success_closure
"""
import functools
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app import db  # noqa: E402
from app import accounts as accounts_mod  # noqa: E402
from app.accounts import _do_sync  # noqa: E402
from app.database import Account, Channel  # noqa: E402
from tests.support import make_test_app  # noqa: E402

CHANNEL_COUNT = 4
M3U_URL = 'http://provider.test/playlist.m3u'


def _locked_commit(*_args, **_kwargs):
    raise OperationalError('COMMIT', {}, Exception('database is locked'))


def one_lock_retry_on_the_success_closure():
    """Patch `retry_on_locked` so only the success closure loses its first commit.

    Matches on the closure's own name, which is unchanged by the fix, so this fixture
    injects identically before and after it.
    """
    real_retry = accounts_mod.retry_on_locked

    def patched_retry(*d_args, **d_kwargs):
        decorator = real_retry(*d_args, **d_kwargs)

        def wrap(func):
            wrapped = decorator(func)
            if 'mark_success_and_commit' not in func.__name__:
                return wrapped
            fired = []

            @functools.wraps(func)
            def run(*args, **kwargs):
                if not fired:
                    fired.append(True)
                    with mock.patch.object(db.session, 'commit', _locked_commit):
                        try:
                            func(*args, **kwargs)
                        except OperationalError:
                            pass
                    db.session.rollback()
                return wrapped(*args, **kwargs)

            return run

        return wrap

    return mock.patch.object(accounts_mod, 'retry_on_locked', patched_retry)


class SuccessClosureRetryTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        # No epg_url: this account's EPG branch is not what is under test, and leaving it
        # out keeps the statement log short enough to assert on directly.
        self.account = Account(name='M3U Provider', account_type='m3u',
                               m3u_url=M3U_URL, status='OK')
        db.session.add(self.account)
        db.session.commit()
        self.account_id = self.account.id

    def tearDown(self):
        self.t.cleanup()

    def _playlist(self):
        lines = ['#EXTM3U']
        for sid in range(1, CHANNEL_COUNT + 1):
            lines.append(f'#EXTINF:-1 tvg-id="ch{sid}.test" tvg-name="Channel {sid}",Channel {sid}')
            lines.append(f'http://provider.test/live/u/p/{sid}.ts')
        return ('\n'.join(lines) + '\n').encode()

    def _sync_with_one_lock_retry(self):
        """Run one sync whose success commit is lost once. Returns (statements, recomputes)."""
        body = self._playlist()

        def fake_get(url, **_kwargs):
            resp = mock.Mock()
            resp.content = body
            resp.raise_for_status.return_value = None
            return resp

        statements = []

        def record(_conn, _cursor, statement, _params, _context, _executemany):
            statements.append(statement)

        recomputes = []
        real_recompute = accounts_mod._recompute_duplicate_stream_urls

        def counting_recompute():
            recomputes.append(True)
            return real_recompute()

        engine = db.session.get_bind()
        event.listen(engine, 'before_cursor_execute', record)
        try:
            with mock.patch.object(accounts_mod, 'requests') as req:
                req.get = fake_get
                with mock.patch.object(accounts_mod, '_recompute_duplicate_stream_urls',
                                       counting_recompute):
                    with one_lock_retry_on_the_success_closure():
                        _do_sync(self.account_id, threading.Event())
        finally:
            event.remove(engine, 'before_cursor_execute', record)
        db.session.expire_all()
        return statements, recomputes

    def _epg_counts(self, statements):
        """The success mark's EPG count - a COUNT joining epg_entries to channels."""
        return [s for s in statements
                if 'count(' in s.lower() and 'epg_entries' in s.lower() and 'JOIN' in s]

    def test_the_epg_count_is_run_once_despite_the_retry(self):
        statements, _ = self._sync_with_one_lock_retry()
        found = self._epg_counts(statements)
        self.assertEqual(len(found), 1,
                         f'the EPG count must not be inside the retried closure, got {found}')

    def test_the_duplicate_recompute_runs_once_despite_the_retry(self):
        _, recomputes = self._sync_with_one_lock_retry()
        self.assertEqual(len(recomputes), 1,
                         'the duplicate-URL recompute must not be inside the retried closure')

    def test_the_counts_are_still_correct_after_the_retry(self):
        """CONTROL: hoisting the reads must not change the values that get stored."""
        self._sync_with_one_lock_retry()
        account = db.session.get(Account, self.account_id)
        self.assertEqual(account.status, 'OK')
        self.assertEqual(account.channel_count, CHANNEL_COUNT)
        self.assertEqual(account.epg_entry_count, 0)

    def test_the_duplicate_flags_are_still_written(self):
        """CONTROL: the recompute still lands, in its own commit rather than the mark's.

        A channel on ANOTHER account already sits on the URL this sync's first channel will
        arrive with, so the sync itself has to flag both - which only happens if the
        recompute ran and its writes were committed.
        """
        other = Account(name='Other', account_type='m3u',
                        m3u_url='http://other.test/p.m3u', status='OK')
        db.session.add(other)
        db.session.commit()
        db.session.add(Channel(account_id=other.id, name='Same Stream', stream_id='99',
                               stream_url='http://provider.test/live/u/p/1.ts'))
        db.session.commit()

        self._sync_with_one_lock_retry()

        db.session.expire_all()
        flagged = Channel.query.filter_by(
            stream_url='http://provider.test/live/u/p/1.ts',
            is_duplicate_stream_url=True).count()
        self.assertEqual(flagged, 2, 'both copies of the shared URL must end the sync flagged')


if __name__ == '__main__':
    unittest.main()
