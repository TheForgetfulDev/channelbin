"""SQLite lock-contention injector for the Class A (retry_on_locked) regression suite.

Deterministically drives `retry_on_locked` by making chosen `db.session.commit()`
invocations raise `OperationalError('database is locked')` before delegating to the real
commit. This lets a test reproduce the exact timing of the historical duplicate-row /
dropped-mutation bugs (BUGS.md 2026-07-11 07:03 PM, 2026-07-16 06:49 AM / 09:12 AM) without
any real concurrency.

Key semantics: `retry_on_locked`'s retry re-invokes the wrapped closure, which calls commit
*again* - a fresh, higher invocation index. So `CommitLockInjector([2])` means "the 2nd commit
is locked once, then the retry's 3rd commit succeeds" - i.e. one transient lock on the 2nd
logical commit. List an index once per transient failure you want on it.

Only ever used against the throwaway DB from make_test_app().
"""
from sqlalchemy.exc import OperationalError

from app import db


def _locked_error():
    # Shape mirrors what SQLite raises; retry_on_locked keys on the 'database is locked'
    # substring (app/db_utils.py::_is_locked_error), not the exception subclass.
    return OperationalError('COMMIT', {}, Exception('database is locked'))


class CommitLockInjector:
    """Context manager patching db.session.commit to raise a locked error on the given
    1-based invocation indices, delegating to the real commit otherwise.

        with CommitLockInjector([2]) as inj:
            ...            # the 2nd commit call raises 'database is locked' once
        inj.calls          # total commit invocations seen
        inj.raised         # how many were made to raise
    """

    def __init__(self, fail_at):
        self.fail_at = list(fail_at)
        self.calls = 0
        self.raised = 0
        self._real = None

    def __enter__(self):
        self._real = db.session.commit

        def patched():
            self.calls += 1
            if self.calls in self.fail_at:
                self.fail_at.remove(self.calls)  # each listed index fires once
                self.raised += 1
                raise _locked_error()
            return self._real()

        db.session.commit = patched
        return self

    def __exit__(self, *exc):
        # Restore by removing the instance attribute so the scoped_session proxy resolves
        # commit through its normal __getattr__ again.
        try:
            del db.session.commit
        except AttributeError:
            db.session.commit = self._real
        return False
