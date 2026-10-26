"""Fixed answers for Readiness checks, shared by the Readiness tests and the setup-step
tests that read the same evaluation."""
import contextlib
from unittest import mock

from app import readiness


@contextlib.contextmanager
def patched(**results):
    """Swap one or more checks for fixed answers, by check id.

    The registry holds a direct reference to each check's function, so patching the module
    attribute would not reach it - the tuple is what has to be replaced.
    """
    swapped = tuple(
        c._replace(run=(lambda result: lambda ctx: result)(results[c.id]))
        if c.id in results else c
        for c in readiness.CHECKS)
    unknown = sorted(set(results) - {c.id for c in readiness.CHECKS})
    assert not unknown, f'no such check: {unknown}'
    with mock.patch.object(readiness, 'CHECKS', swapped), \
            mock.patch.dict(readiness.CHECKS_BY_ID, {c.id: c for c in swapped}):
        yield


def result(status, found='found', tested='tested'):
    return readiness.Result(status, tested, found)
