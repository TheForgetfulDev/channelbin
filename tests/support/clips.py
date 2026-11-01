"""Synthesized media sources, built once per process and shared by every class that asks.

A module that drives real ffmpeg over small synthesized files builds them in a base class's
`setUpClass`, and every class that inherited the base used to build the same set again: the
six classes of `test_normalize_conversion` encoded the same nine files, 42 encodes a run for
seven distinct ones, and `test_converted_output_check`, `test_conversion_gap_fill` and
`test_format_pin_recheck` had the same shape (dev/changelog/1350). `shared(key, build)`
runs `build(directory)` the first time a key is asked for in this process and hands every
later caller the same directory.

The contract is read-only: a class copies or probes out of the directory and never writes
into it, so a class that runs later sees exactly what `build` made. The directory is removed
at process exit; a worker that dies leaves it behind, exactly as a per-class tempdir did.
"""
import atexit
import shutil
import tempfile
import threading

_lock = threading.Lock()
_dirs = {}


def shared(key, build, prefix='clips-'):
    """The directory `build(dir)` filled for `key`, built on the first call in this process.

    A `build` that raises leaves nothing behind and the next caller tries again, so a class
    that fails to build its sources fails on its own setUpClass rather than handing a
    half-built directory to the classes after it.
    """
    with _lock:
        d = _dirs.get(key)
        if d is None:
            d = tempfile.mkdtemp(prefix=prefix)
            try:
                build(d)
            except BaseException:
                shutil.rmtree(d, ignore_errors=True)
                raise
            atexit.register(shutil.rmtree, d, ignore_errors=True)
            _dirs[key] = d
        return d
