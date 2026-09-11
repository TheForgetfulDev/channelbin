"""Single source of truth for the app version.

Semver, pre-1.0: MINOR bumps may include breaking changes (standard semver-0 convention).
1.0.0 is reserved for the announced release, not the first public one - the repo goes public
at 0.4.0 without an announcement.
Post-1.0: MAJOR = may need manual intervention, MINOR = features (startup auto-migration
handles any schema/config changes), PATCH = fixes.

The DB schema version (PRAGMA user_version, see app/migrations.py) is an independent
monotonic integer and does NOT track this number.
"""

__version__ = '0.6.0'
