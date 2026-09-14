"""Filesystem-path probing that distinguishes "not there" from "not reachable".

The canonical home for every "can I use this directory, and if not, why?" question.
`/dvr` on a network mount can answer ESTALE on its own root while paths *through* it
still resolve, and the stdlib's convenience predicates hide that: `os.path.isdir()`
catches every OSError and answers False, so an unreachable mount is indistinguishable
from a missing directory, and `os.makedirs(exist_ok=True)` re-raises the mkdir's EEXIST
because its own isdir() recheck was the thing that got lied to. Both behaviors verified
on this machine against Python 3.12 (dev/changelog/723).

Product principle 1: the reason a user reads has to name what actually happened. "File
exists" and "does not exist" are both false when the answer is "the NAS is gone".
"""
import errno
import logging
import os
import stat
from collections import namedtuple

log = logging.getLogger(__name__)

# Every outcome probe_dir() can return. Callers branch over these explicitly - a
# trailing `else` here would silently absorb whatever state is added next.
PATH_OK = 'ok'                    # an existing, listable directory
PATH_MISSING = 'missing'          # nothing at this path (or a parent component is absent)
PATH_NOT_A_DIR = 'not_a_dir'      # something is there, but it is not a directory
PATH_DENIED = 'denied'            # it is there; this process may not look at it
PATH_UNREACHABLE = 'unreachable'  # the storage layer itself failed to answer

# The storage-layer errnos: the mount is stale, the server is gone, the transport
# broke. What separates these from ENOENT is that the path may well still exist -
# nothing here is evidence about its contents, only about the link to them.
UNREACHABLE_ERRNOS = frozenset(e for e in (
    getattr(errno, name, None) for name in (
        'ESTALE', 'EIO', 'ENOTCONN', 'EHOSTDOWN', 'EHOSTUNREACH',
        'ETIMEDOUT', 'EREMOTEIO', 'ENETDOWN', 'ENETUNREACH', 'ECONNABORTED',
        'ECONNRESET', 'ENOLINK', 'ENODEV', 'ESHUTDOWN',
    )
) if e is not None)

DirProbe = namedtuple('DirProbe', 'outcome errno strerror')


class PathUnusableError(OSError):
    """A configured directory cannot be used, carrying a message that names why.

    str() is the human sentence from describe_dir_problem(), so a caller that already
    funnels failures through `except Exception as exc: ... str(exc)` gets the good
    message without growing a second error path.
    """


def classify_oserror(exc: OSError) -> DirProbe:
    """Turn an OSError raised against a path into the DirProbe that describes it.

    Shared by probe_dir() and by callers whose own syscall (statvfs, listdir) failed
    after the path itself probed fine, so one errno means one outcome everywhere.
    """
    strerror = exc.strerror or str(exc)
    if exc.errno in UNREACHABLE_ERRNOS:
        return DirProbe(PATH_UNREACHABLE, exc.errno, strerror)
    if exc.errno in (errno.EACCES, errno.EPERM):
        return DirProbe(PATH_DENIED, exc.errno, strerror)
    if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ENAMETOOLONG, errno.ELOOP):
        return DirProbe(PATH_MISSING, exc.errno, strerror)
    # An errno nobody has classified yet. "Unreachable" is the safe side: it stops a
    # caller from creating or trusting a path whose state we genuinely do not know, and
    # it says so out loud instead of pretending the path is simply absent.
    return DirProbe(PATH_UNREACHABLE, exc.errno, strerror)


def probe_dir(path) -> DirProbe:
    """Classify `path` as one of the PATH_* outcomes. Never raises.

    Uses os.stat directly rather than os.path.isdir/exists because those collapse
    every OSError into a bare False, which is the whole defect this module exists to
    undo.
    """
    if not path:
        return DirProbe(PATH_MISSING, None, 'no path configured')
    try:
        st = os.stat(path)
    except OSError as exc:
        return classify_oserror(exc)
    # stat.S_ISDIR on the result we already have, not a second os.path.isdir() call -
    # that would re-enter the same error-swallowing predicate this function replaces.
    if not stat.S_ISDIR(st.st_mode):
        return DirProbe(PATH_NOT_A_DIR, None, 'not a directory')
    return DirProbe(PATH_OK, None, None)


def describe_dir_problem(path, probe: DirProbe) -> str:
    """One sentence naming what is wrong with `path`, for an event, alert or log line."""
    if probe.outcome == PATH_OK:
        return f'{path} is available'
    if probe.outcome == PATH_MISSING:
        return f'{path} does not exist'
    if probe.outcome == PATH_NOT_A_DIR:
        return f'{path} exists but is not a directory'
    if probe.outcome == PATH_DENIED:
        return f'{path} is not accessible ({probe.strerror})'
    if probe.outcome == PATH_UNREACHABLE:
        return (f'{path} is not reachable ({probe.strerror}) - the storage it lives on '
                f'is down or the mount is stale; the path itself may still be intact')
    # Not a real outcome: PATH_* is a closed set and every member is named above.
    return f'{path} is unusable ({probe.outcome})'


def ensure_dir(path) -> DirProbe:
    """Return an OK probe for a usable directory, creating it if it is merely missing.

    Raises PathUnusableError - whose str() is describe_dir_problem() - when the path
    cannot be made usable. Callers get "the destination is not reachable" instead of
    makedirs' EEXIST, which is what a stale mount produces there.
    """
    probe = probe_dir(path)
    if probe.outcome == PATH_OK:
        return probe
    if probe.outcome == PATH_MISSING:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            # Lost a race with another creator, or the parent went away between the
            # probe and the mkdir. Re-probe so the message describes the path's state
            # now rather than the failed attempt.
            probe = probe_dir(path)
            if probe.outcome == PATH_OK:
                return probe
            raise PathUnusableError(describe_dir_problem(path, probe)) from exc
        probe = probe_dir(path)
        if probe.outcome == PATH_OK:
            return probe
    raise PathUnusableError(describe_dir_problem(path, probe))


# Path -> the last outcome logged for it. Hot callers (the sidebar polls the disk
# readout every 15s per open tab) must not emit the same warning forever, but the
# transition into and out of trouble is exactly what an operator needs timestamped.
# Reset between tests via tests/support/app.py::reset_module_globals.
#: Filesystem types that are a network share however they were mounted. CLAUDE.md forbids
#: putting a SQLite database on one of these: /dvr answered `ls` with "Stale file handle"
#: and SQLite with "unable to open database file" mid-task on 2026-08-15, and a DB there
#: loses writes and locks unpredictably rather than failing loudly.
#:
#: Matched against /proc/mounts' own third field, so these are kernel filesystem names
#: rather than anything this app chooses. `fuse.sshfs` and friends carry a subtype, so the
#: comparison also takes the part before the first dot.
NETWORK_FILESYSTEMS = frozenset((
    'cifs', 'smbfs', 'smb3', 'nfs', 'nfs4', 'afs', 'ncpfs', 'coda', 'glusterfs',
    'ceph', 'lustre', 'beegfs', 'afpfs', 'davfs', 'sshfs', 'ftpfs', '9p',
))

_last_logged_outcome: dict = {}


def filesystem_type(path):
    """The filesystem type `path` sits on, read from /proc/mounts, or None.

    Resolved by longest matching mount point rather than by an exact hit, because the
    answer for `/config/dvr.db` is whatever `/config`, `/` or anything between them is
    mounted as. None means the question could not be answered at all - there is no
    /proc/mounts (a non-Linux host), or nothing in it covers the path - and is never
    reported as "local", since a filesystem nobody could name is not evidence of anything.

    Deliberately not os.statvfs: f_fsid and f_type are not exposed by statvfs in Python,
    and the mount table is the thing that actually names cifs vs ext4.
    """
    if not path:
        return None
    target = os.path.abspath(path)
    best = None
    best_len = -1
    try:
        with open('/proc/mounts', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                point, fstype = parts[1].replace('\\040', ' '), parts[2]
                if target == point or target.startswith(point.rstrip('/') + '/'):
                    if len(point) > best_len:
                        best, best_len = fstype, len(point)
    except OSError as exc:
        log.warning('could not read /proc/mounts to place %s: %s', path, exc)
        return None
    return best


def is_network_filesystem(fstype) -> bool:
    """Whether a /proc/mounts filesystem name is a network share. False for None."""
    if not fstype:
        return False
    return fstype in NETWORK_FILESYSTEMS or fstype.split('.', 1)[0] in NETWORK_FILESYSTEMS


def log_dir_outcome_change(path, probe: DirProbe, what: str) -> bool:
    """Log a WARNING when `path` enters a bad state and an INFO when it recovers.

    Silent while the outcome is unchanged, so this is safe on a polling path. `what`
    names the reader's stake in it ('DVR output directory'), since the path alone does
    not say what stopped working.

    Returns True when the outcome actually moved. That is the transition a second
    surface can hang off without paying for the poll: the disk readout behind this runs
    every 15s per open browser tab, so app/routes/system.py gates the standing
    STORAGE_PATH_UNUSABLE alert on this answer rather than re-deciding per poll
    (dev/changelog/868).
    """
    previous = _last_logged_outcome.get(path)
    if previous == probe.outcome:
        return False
    _last_logged_outcome[path] = probe.outcome
    if probe.outcome == PATH_OK:
        if previous is not None:
            log.info('%s %s is reachable again', what, path)
        return True
    log.warning('%s unusable: %s', what, describe_dir_problem(path, probe))
    return True
