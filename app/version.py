"""Single source of truth for the app version.

Semver, pre-1.0: MINOR bumps may include breaking changes (standard semver-0 convention).
1.0.0 is reserved for the announced release, not the first public one - the repo goes public
at 0.4.0 without an announcement.
Post-1.0: MAJOR = may need manual intervention, MINOR = features (startup auto-migration
handles any schema/config changes), PATCH = fixes.

The DB schema version (PRAGMA user_version, see app/migrations.py) is an independent
monotonic integer and does NOT track this number.

An in-between image (not a release) carries a build stamp, written into BUILD_STAMP next to
this file by the Dockerfile's CHANNELBIN_BUILD build argument, and reports itself as
`<release>+<stamp>` - semver build metadata, so it still compares equal to the release it was
built after rather than sorting before it as a `-` pre-release would. A checkout and a
release image have no stamp and report the bare release (dev/changelog/1622).

The release number stays on its own literal line below: the release procedure and several
dev tools find it with `^__version__\\s*=\\s*'...'`, which the `+=` line does not match.
"""
import os


def _build_stamp():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'BUILD_STAMP'),
                  encoding='utf-8') as fh:
            stamp = fh.read().strip()
    except FileNotFoundError:
        return ''
    return '+' + stamp if stamp else ''


__version__ = '0.45.0'
__version__ += _build_stamp()
