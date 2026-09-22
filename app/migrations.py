"""Versioned schema migration runner.

The authoritative schema version stamp is SQLite's `PRAGMA user_version` (0 = pre-versioning
DB); the schema_migrations table (see database.SchemaMigration) is an audit log only.
Division of labor with db.create_all():

- New tables: define the model in database.py - create_all() (which runs before
  run_migrations()) creates any table that doesn't exist. No migration step needed.
- Everything else (ALTER/ADD COLUMN, drops, renames, data transforms, backfills): append one
  entry to SCHEMA_MIGRATIONS with the next integer version. Steps run in order at startup,
  each stamped into user_version as it commits, and are never edited once shipped.

Steps should still be written in the idempotent guarded style (check PRAGMA table_info before
ALTER) as defense in depth - the version stamp provides ordering and skip logic, idempotency
is the safety net if a step is interrupted between its own commit and the stamp.

A step that backfills data behind a column it just added gates that backfill on the backfill
ledger below, never on "did I add the column just now" - see that section's comment for why
the second one silently loses data on an interrupted upgrade (dev/changelog/686).

An INSERT inside a step runs against a table of unknown vintage, because create_all() builds
any table that does not exist yet in the CURRENT model's shape before the runner starts. So a
step's column list has to be derived from PRAGMA table_info rather than frozen at the time the
step was written: name a column a later model dropped and the step dies with "no such column"
on a table create_all() just built, omit a NOT NULL column a later model added (SQLAlchemy's
default= is Python-side and never becomes a SQL DEFAULT) and it dies on the constraint. Both
abort startup with no way past them short of a code change. _m004_unify_guide_health_checks
carries the worked example (dev/changelog/687).

The second of those two is closed from the other end as well: an ADD COLUMN here that carries
a SQL DEFAULT must be matched by an equal server_default on the model column, so the fresh
build and the migrated database end up with the same DDL rather than only the migrated one
carrying the DEFAULT. Adding the column without it fails
tests/test_static_invariants.py::MigrationServerDefaultParityTests (dev/changelog/690).

"Never edit a shipped step" has exactly one exception, and it is narrow: an edit is allowed
when it cannot change the outcome for a database that already crossed the step. A stamped DB
never runs the step again, so a change to how the step recovers from its own interruption is
invisible to every already-migrated install and only reaches DBs still below that version.
Anything that would make a crossed DB's data different from what the shipped step produced is
a new step at the next version, not an edit.

A step that calls current application code extends that rule outward, because the step's own
source is then no longer the whole of what it does - editing the callee edits the step. This
is allowed in exactly one case: when the step's output is *defined* as "whatever the current
code would produce," not merely when the current code happens to compute the right answer
today. url_normalizable is defined as accounts.url_is_normalizable(url), and the search
indexes are defined as whatever search_index.py currently builds, so a step that froze an era
copy of either would stamp a value the running app then disagrees with - the pinned version
is the bug, not the fix. A value that must instead be reproducible after the fact has to have
its logic written inside the step. _m004 is the one dependency of a different kind: it reads
legacy channel_testing keys out of config.yaml, which is input state like the database rather
than code (dev/changelog/688).

Two consequences for whoever edits such a callee. Its output for databases that have not
crossed the step yet is part of the change, and is usually fine under the definition above -
but it must still leave the step *runnable* against a table of the vintage that step meets,
which is the failure mode that actually breaks a startup rather than merely moving a value.
_ensure_search_index_shape is the in-tree mitigation: it exists solely so three shipped steps
can keep calling the current rebuild SQL against an older-shaped chan_prog. The current
callees are enumerated in tests/test_migrations_runner.py::CurrentCodeDependencyTests, which
fails when a new step reaches for app code without being declared there.

Rollback posture: there are no down-migrations. Before any pending step runs, the DB is
snapshotted via VACUUM INTO (config: database.pre_migration_backup / backup_dir /
migration_backups_keep, the last two read through config.db_backup_dir so the snapshots
belong to the database being migrated); recovery = stop the app, restore that snapshot over
dvr.db (removing
any -wal/-shm files), and run the matching older code.

Startup-only and single-threaded, so commits here deliberately skip retry_on_locked - no
concurrent writer exists yet (same exemption as _seed_default_tags in app/__init__.py).
"""
import glob
import logging
import os
import re
import shutil
import time
from datetime import datetime

from . import db
from .version import __version__

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backfill obligation ledger
# ---------------------------------------------------------------------------
#
# A step that adds a column and then populates it has to commit the ADD COLUMN first: an ORM
# backfill opens its own connection and would deadlock against this one's open write
# transaction, and a chunked backfill commits as it goes. That leaves a window - crash after
# the column commit but before the version stamp - in which the retry finds the column
# already present.
#
# Gating the backfill on "did I add this column just now" reads that state as "already done",
# skips the backfill for good, and stamps the version, so nothing ever revisits it: the column
# exists, its data is wrong, and no log line or alert says so. That is the silent partial
# failure this app exists to refuse, and it is why the obligation is written to the database
# instead of inferred from the schema.
#
# The obligation is committed BEFORE the ALTER it belongs to, so the only crash window puts a
# pending obligation next to a column that does not exist yet - which the retry handles - and
# never a column with no obligation. It is cleared only once the backfill has finished.
#
# The ledger is a plain table with no ORM model on purpose: migration steps also run against
# bare scratch connections that carry nothing create_all() built (tests/test_migrations_runner.py),
# so the facility has to create its own storage wherever it is used.
#
# A backfill reached through this ledger may be re-run from the top after a partial attempt,
# so each one must be a recompute rather than an increment. _backfill_health_scores is the
# one that needed changing to satisfy that - see its own docstring.

_BACKFILL_LEDGER_DDL = (
    'CREATE TABLE IF NOT EXISTS migration_backfills ('
    ' name TEXT PRIMARY KEY,'
    ' registered_at DATETIME NOT NULL,'
    ' completed_at DATETIME)'
)

# Ledger names are the stored contract - a rename orphans a pending obligation on any DB that
# is mid-upgrade, so append new ones rather than re-spelling these.
_BF_URL_NORMALIZATION = 'm001.url_normalization'
_BF_DUPLICATE_FLAGS = 'm001.duplicate_flags'
_BF_HEALTH_SCORES = 'm001.health_scores'
_BF_URL_NORMALIZABLE = 'm024.url_normalizable'
_BF_FAILURE_STREAK = 'm026.consecutive_test_failures'
_BF_SEGMENT_CHANNEL = 'm032.segment_channel_id'
_BF_SYNC_LOG_SKIP_COUNTS = 'm051.sync_log_skip_counts'
_BF_ANALYSIS_COMPLETED = 'm057.analysis_completed_at'
#: Not a column backfill - an obligation to repair channel health scores that a repeated
#: analysis phase double-counted, discharged by health_recompute.repair_duplicated_capture_
#: corrections() from create_app() once the ORM is available. The ledger is used for the
#: same reason a column backfill uses it: the obligation is committed before the work, so
#: an interrupted repair is retried rather than inferred complete (dev/changelog/951).
_BF_DUPLICATE_CAPTURE_CORRECTIONS = 'm057.duplicate_capture_corrections'


def _register_backfill(conn, cur, name: str):
    """Record that `name` still owes a backfill, and commit it.

    Call this immediately before the ALTER whose data it populates, never after: committing
    the obligation first is what guarantees no crash can leave the column present with its
    obligation lost, which is the whole defect being closed here.
    """
    cur.execute(_BACKFILL_LEDGER_DDL)
    cur.execute('INSERT OR IGNORE INTO migration_backfills (name, registered_at) VALUES (?, ?)',
                (name, _utc_stamp()))
    conn.commit()


def _backfill_pending(cur, name: str) -> bool:
    cur.execute(_BACKFILL_LEDGER_DDL)
    row = cur.execute('SELECT completed_at FROM migration_backfills WHERE name = ?',
                      (name,)).fetchone()
    return row is not None and row[0] is None


def _backfill_needed(cur, name: str, registered_now: bool) -> bool:
    """Whether `name`'s backfill still has to run. Names an interrupted earlier attempt in the
    log when it is resuming one rather than running it for the first time - an upgrade that
    died partway through is exactly the thing that must not pass silently."""
    if not _backfill_pending(cur, name):
        return False
    if not registered_now:
        log.warning('Backfill %s was registered by an earlier migration attempt that did not '
                    'finish - running it again now.', name)
    return True


def _finish_backfill(conn, cur, name: str):
    cur.execute('UPDATE migration_backfills SET completed_at = ? WHERE name = ?',
                (_utc_stamp(), name))
    conn.commit()


def obligation_pending(name: str) -> bool:
    """Whether `name`'s ledger entry is registered and not yet finished, asked through the
    ORM's own connection rather than a raw migration cursor.

    For an obligation a migration step registers but cannot discharge itself, because the
    work is ORM code that only exists once create_app() has built the models - see
    _BF_DUPLICATE_CAPTURE_CORRECTIONS. The gating rule is the same one the private helpers
    above enforce: the obligation is a recorded fact, never inferred from whether the work
    looks done.
    """
    from sqlalchemy import text
    # Asked of sqlite_master rather than by running the CREATE TABLE IF NOT EXISTS the private
    # helpers use: this runs on every startup for the life of the obligation, and a read has
    # no business taking a write lock to answer a question about a table a fresh database
    # legitimately does not have yet.
    present = db.session.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_backfills'"
    )).fetchone()
    if present is None:
        return False
    row = db.session.execute(
        text('SELECT completed_at FROM migration_backfills WHERE name = :n'),
        {'n': name}).fetchone()
    return row is not None and row[0] is None


def finish_obligation(name: str):
    """Mark `name` discharged. Executes; does NOT commit - the caller owns the commit so the
    work and its completion stamp land as one retry_on_locked unit."""
    from sqlalchemy import text
    db.session.execute(
        text('UPDATE migration_backfills SET completed_at = :t WHERE name = :n'),
        {'t': _utc_stamp(), 'n': name})


def _utc_stamp() -> str:
    """Naive-UTC timestamp as a string. Written rather than handed to sqlite3's datetime
    adapter (deprecated in Python 3.12) because nothing reads these back as datetimes -
    the ledger is only ever asked whether completed_at is NULL."""
    return datetime.utcnow().isoformat(sep=' ', timespec='seconds')


# ---------------------------------------------------------------------------
# Migration steps - append only, never edit a shipped step (one narrow exception,
# see the module docstring)
# ---------------------------------------------------------------------------

def _m001_baseline(conn, cur):
    """Baseline: every additive migration + backfill from the pre-versioning era
    (formerly _migrate_db() in app/__init__.py). Fully idempotent, so it is safe on any
    pre-versioning DB vintage - on a fully up-to-date DB it is a near-no-op."""
    existing = [r[1] for r in cur.execute('PRAGMA table_info(accounts)').fetchall()]
    migrations = [
        ('account_type', "VARCHAR(32) NOT NULL DEFAULT 'm3u'"),
        ('m3u_url',      'VARCHAR(2048)'),
        ('epg_url',      'VARCHAR(2048)'),
    ]
    for col, definition in migrations:
        if col not in existing:
            cur.execute(f'ALTER TABLE accounts ADD COLUMN {col} {definition}')

    epg_existing = [r[1] for r in cur.execute('PRAGMA table_info(epg_entries)').fetchall()]
    if 'sub_title' not in epg_existing and epg_existing:
        cur.execute('ALTER TABLE epg_entries ADD COLUMN sub_title VARCHAR(512)')

    ch_existing = [r[1] for r in cur.execute('PRAGMA table_info(channels)').fetchall()]
    ch_raw_url_added = bool(ch_existing) and 'raw_stream_url' not in ch_existing
    ch_dup_flag_added = bool(ch_existing) and 'is_duplicate_stream_url' not in ch_existing
    health_score_added = bool(ch_existing) and 'health_score' not in ch_existing
    # Registered before their ALTERs, per the ledger's own rule. A pre-versioning DB that the
    # old _migrate_db() already brought up to date registers nothing here, which is what keeps
    # its already-computed scores and flags from being recomputed on the way to version 1.
    if ch_raw_url_added:
        _register_backfill(conn, cur, _BF_URL_NORMALIZATION)
    if ch_dup_flag_added:
        _register_backfill(conn, cur, _BF_DUPLICATE_FLAGS)
    if health_score_added:
        _register_backfill(conn, cur, _BF_HEALTH_SCORES)

    if ch_existing and 'test_enabled' not in ch_existing:
        cur.execute('ALTER TABLE channels ADD COLUMN test_enabled BOOLEAN NOT NULL DEFAULT 1')
    if ch_raw_url_added:
        cur.execute('ALTER TABLE channels ADD COLUMN raw_stream_url VARCHAR(2048)')
        cur.execute('UPDATE channels SET raw_stream_url = stream_url WHERE raw_stream_url IS NULL')
    if ch_existing and 'notes' not in ch_existing:
        cur.execute('ALTER TABLE channels ADD COLUMN notes TEXT')
    if ch_dup_flag_added:
        cur.execute('ALTER TABLE channels ADD COLUMN is_duplicate_stream_url BOOLEAN NOT NULL DEFAULT 0')
    if health_score_added:
        cur.execute('ALTER TABLE channels ADD COLUMN health_score REAL')
        cur.execute('ALTER TABLE channels ADD COLUMN health_score_sample_count INTEGER NOT NULL DEFAULT 0')
        cur.execute('ALTER TABLE channels ADD COLUMN health_score_updated_at DATETIME')
        cur.execute('ALTER TABLE channels ADD COLUMN manual_health_adjustment INTEGER NOT NULL DEFAULT 0')
        cur.execute('ALTER TABLE channels ADD COLUMN manual_health_note TEXT')
    if ch_existing and 'default_profile_id' not in ch_existing:
        cur.execute('ALTER TABLE channels ADD COLUMN default_profile_id INTEGER REFERENCES recording_profiles(id)')

    ct_existing = [r[1] for r in cur.execute('PRAGMA table_info(channel_tests)').fetchall()]
    if ct_existing:
        ct_migrations = [
            ('duration_seconds',   'REAL'),
            ('connect_attempts',   'INTEGER DEFAULT 1'),
            ('audio_codec',        'VARCHAR(64)'),
            ('audio_channels',     'INTEGER'),
            ('audio_sample_rate',  'INTEGER'),
            ('audio_bitrate_kbps', 'REAL'),
            ('audio_language',     'VARCHAR(32)'),
            ('job_id',             'INTEGER REFERENCES on_demand_test_jobs(id)'),
            ('frame_count',        'INTEGER'),
            ('frame_pct',          'REAL'),
            ('screenshot_pruned',  'BOOLEAN NOT NULL DEFAULT 0'),
            ('quality_score',      'INTEGER'),
            ('lifetime_score_after', 'INTEGER'),
            ('quality_breakdown',  'TEXT'),
            ('blend_breakdown',    'TEXT'),
        ]
        for col, definition in ct_migrations:
            if col not in ct_existing:
                cur.execute(f'ALTER TABLE channel_tests ADD COLUMN {col} {definition}')

    rec_existing = [r[1] for r in cur.execute('PRAGMA table_info(recordings)').fetchall()]
    if rec_existing:
        rec_migrations = [
            ('channel_id',                'INTEGER'),
            ('channel_health_snapshot',   'TEXT'),
            ('recorded_resolution',       'VARCHAR(32)'),
            ('recorded_fps',              'REAL'),
            ('recorded_frame_count',      'INTEGER'),
            ('recorded_duration_seconds', 'REAL'),
            ('recorded_frame_pct',        'REAL'),
            ('recorded_bitrate_kbps',     'REAL'),
            ('recorded_audio_codec',      'VARCHAR(64)'),
            ('recorded_audio_channels',   'INTEGER'),
            ('health_gathered_at',        'DATETIME'),
            ('scheduled_start_time',      'DATETIME'),
            ('scheduled_stop_time',       'DATETIME'),
            ('program_start_time',        'DATETIME'),
            ('program_stop_time',         'DATETIME'),
            ('health_quality_score',      'INTEGER'),
            ('health_quality_breakdown',  'TEXT'),
            ('health_blend_breakdown',    'TEXT'),
            ('failure_reason',            'VARCHAR(64)'),
            ('profile_id',                'INTEGER REFERENCES recording_profiles(id)'),
        ]
        for col, definition in rec_migrations:
            if col not in rec_existing:
                cur.execute(f'ALTER TABLE recordings ADD COLUMN {col} {definition}')

    rp_existing = [r[1] for r in cur.execute('PRAGMA table_info(recording_profiles)').fetchall()]
    if rp_existing and 'pre_padding_minutes' not in rp_existing:
        cur.execute('ALTER TABLE recording_profiles ADD COLUMN pre_padding_minutes INTEGER NOT NULL DEFAULT 0')
        cur.execute('ALTER TABLE recording_profiles ADD COLUMN post_padding_minutes INTEGER NOT NULL DEFAULT 0')
        if 'pre_padding_seconds' in rp_existing:
            # Padding was originally stored in seconds; convert existing values to minutes.
            cur.execute(
                'UPDATE recording_profiles SET '
                'pre_padding_minutes = CAST(ROUND(pre_padding_seconds / 60.0) AS INTEGER), '
                'post_padding_minutes = CAST(ROUND(post_padding_seconds / 60.0) AS INTEGER)'
            )
        rp_existing.append('pre_padding_minutes')
    if rp_existing and 'pre_padding_seconds' in rp_existing:
        # Old seconds columns are NOT NULL with no DB-level default and the model no
        # longer populates them, so leaving them in place breaks every future insert -
        # see dev/docs/BUGS.md 2026-07-15 "profile insert fails after padding unit change".
        cur.execute('ALTER TABLE recording_profiles DROP COLUMN pre_padding_seconds')
        cur.execute('ALTER TABLE recording_profiles DROP COLUMN post_padding_seconds')

    odj_existing = [r[1] for r in cur.execute('PRAGMA table_info(on_demand_test_jobs)').fetchall()]
    if odj_existing and 'disabled_channel_ids_json' not in odj_existing:
        cur.execute('ALTER TABLE on_demand_test_jobs ADD COLUMN disabled_channel_ids_json TEXT')
    if odj_existing and 'recurring' not in odj_existing:
        cur.execute('ALTER TABLE on_demand_test_jobs ADD COLUMN recurring BOOLEAN NOT NULL DEFAULT 0')
        cur.execute('ALTER TABLE on_demand_test_jobs ADD COLUMN recur_day INTEGER')
        cur.execute('ALTER TABLE on_demand_test_jobs ADD COLUMN recur_hour INTEGER')
        cur.execute('ALTER TABLE on_demand_test_jobs ADD COLUMN recur_minute INTEGER')
    if odj_existing and 'recur_paused' not in odj_existing:
        cur.execute('ALTER TABLE on_demand_test_jobs ADD COLUMN recur_paused BOOLEAN NOT NULL DEFAULT 0')
        cur.execute('ALTER TABLE on_demand_test_jobs ADD COLUMN status_before_schedule VARCHAR(32)')
    if odj_existing and 'profile_id' not in odj_existing:
        cur.execute('ALTER TABLE on_demand_test_jobs ADD COLUMN profile_id INTEGER REFERENCES health_check_profiles(id)')

    if rec_existing and 'total_bytes_recorded' in rec_existing:
        # Dead column - never read or written anywhere (dropped 2026-07-16).
        cur.execute('ALTER TABLE recordings DROP COLUMN total_bytes_recorded')

    # RECORDING_COMPLETE was renamed CAPTURE_COMPLETE 2026-07-16 (it fires at concat
    # start, not when the recording is fully done). Idempotent - matches 0 rows after
    # the first run.
    cur.execute(
        "UPDATE recording_events SET event_type = 'CAPTURE_COMPLETE' "
        "WHERE event_type = 'RECORDING_COMPLETE'"
    )

    # The ORM backfills below open their own connection (db.session), which would hit
    # "database is locked" against this connection's still-open write transaction -
    # commit the raw-SQL half first. Each one is then gated on its ledger obligation rather
    # than on the *_added flags, so a run interrupted after this commit finishes the job on
    # the next startup instead of skipping it forever.
    conn.commit()
    if _backfill_needed(cur, _BF_URL_NORMALIZATION, ch_raw_url_added):
        _backfill_url_normalization()
        _finish_backfill(conn, cur, _BF_URL_NORMALIZATION)
    if _backfill_needed(cur, _BF_DUPLICATE_FLAGS, ch_dup_flag_added):
        _backfill_duplicate_flags()
        _finish_backfill(conn, cur, _BF_DUPLICATE_FLAGS)
    if _backfill_needed(cur, _BF_HEALTH_SCORES, health_score_added):
        _backfill_health_scores()
        _finish_backfill(conn, cur, _BF_HEALTH_SCORES)


def _m002_program_title(conn, cur):
    """Add program_title/program_sub_title snapshot columns to recordings, then best-effort
    backfill from EPG entries still matching (channel_id, program_start_time). Older
    recordings whose entries were pruned (sync.epg_keep_days) simply stay NULL - the
    "find another airing" UI falls back to the recording name for those.

    Both subqueries order by e.id so a channel with duplicate entries at the same start_time
    (epg_entries has no uniqueness on (channel_id, start_time)) resolves title and sub_title
    from the same row - without it, SQL gives no guarantee the two LIMIT 1 subqueries agree,
    and a divergent index choice between them can silently attribute the two columns to two
    different EPG entries."""
    existing = [r[1] for r in cur.execute('PRAGMA table_info(recordings)').fetchall()]
    for col in ('program_title', 'program_sub_title'):
        if col not in existing:
            cur.execute(f'ALTER TABLE recordings ADD COLUMN {col} VARCHAR(512)')
    cur.execute(
        'UPDATE recordings SET '
        'program_title = (SELECT e.title FROM epg_entries e '
        '  WHERE e.channel_id = recordings.channel_id '
        '    AND e.start_time = recordings.program_start_time ORDER BY e.id LIMIT 1), '
        'program_sub_title = (SELECT e.sub_title FROM epg_entries e '
        '  WHERE e.channel_id = recordings.channel_id '
        '    AND e.start_time = recordings.program_start_time ORDER BY e.id LIMIT 1) '
        'WHERE program_title IS NULL '
        '  AND program_start_time IS NOT NULL AND channel_id IS NOT NULL'
    )


def _m003_channel_group_columns(conn, cur):
    """Manual channel grouping: group_id FK columns on channels and recordings.
    The channel_groups table itself is created by db.create_all() (new table)."""
    for table in ('channels', 'recordings'):
        existing = [r[1] for r in cur.execute(f'PRAGMA table_info({table})').fetchall()]
        if 'group_id' not in existing:
            cur.execute(f'ALTER TABLE {table} ADD COLUMN group_id INTEGER')


def _m004_unify_guide_health_checks(conn, cur):
    """Guide Channels becomes a pinned system OnDemandTestJob row ('TV Guide Channels'):
    is_system column, the system row itself (schedule copied from the legacy
    channel_testing.enabled/schedule_hour/test_days config keys, which _ensure_system_health_job
    in app/__init__.py removes from config.yaml after this runs), historical guide tests
    (job_id NULL) backfilled onto it, and the legacy channel_testing_daily APScheduler job
    dropped from the jobstore (its function no longer exists)."""
    existing = [r[1] for r in cur.execute('PRAGMA table_info(on_demand_test_jobs)').fetchall()]
    if 'is_system' not in existing:
        cur.execute('ALTER TABLE on_demand_test_jobs ADD COLUMN is_system BOOLEAN NOT NULL DEFAULT 0')
        existing.append('is_system')

    row = cur.execute('SELECT id FROM on_demand_test_jobs WHERE is_system = 1').fetchone()
    if row is None:
        # load_config() still surfaces the legacy keys here even though they're gone from
        # _DEFAULTS - deep-merge keeps unknown file keys, and config-key removal happens
        # after migrations (see _ensure_system_health_job ordering note).
        from .config import load_config
        ct_cfg = load_config().get('channel_testing', {})
        # Both table vintages reach this INSERT, so the column list is filtered against what
        # the table actually has rather than frozen (see the module docstring): a DB old
        # enough to predate on_demand_test_jobs has it built by CURRENT create_all(), where
        # channel_ids_json is long gone (_m011) and recur_use_window is NOT NULL with no SQL
        # DEFAULT (_m029). On the era shape this produces the original statement unchanged.
        cols = [
            ('name',              'TV Guide Channels'),
            ('is_system',         1),
            ('status',            'SCHEDULED'),
            ('created_at',        datetime.utcnow()),
            ('recurring',         1),
            ('recur_day',         ct_cfg.get('test_days', 0)),
            ('recur_hour',        ct_cfg.get('schedule_hour', 2)),
            ('recur_minute',      0),
            ('recur_paused',      0 if ct_cfg.get('enabled', True) else 1),
            ('recur_use_window',  0),
            ('channel_ids_json',  '[]'),
        ]
        cols = [(name, value) for name, value in cols if name in existing]
        cur.execute(
            'INSERT INTO on_demand_test_jobs ({}) VALUES ({})'.format(
                ', '.join(name for name, _ in cols),
                ', '.join('?' for _ in cols),
            ),
            [value for _, value in cols],
        )
        system_id = cur.lastrowid
    else:
        system_id = row[0]

    cur.execute('UPDATE channel_tests SET job_id = ? WHERE job_id IS NULL', (system_id,))

    aps = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='apscheduler_jobs'"
    ).fetchone()
    if aps:
        cur.execute("DELETE FROM apscheduler_jobs WHERE id = 'channel_testing_daily'")


def _m005_channel_group_health(conn, cur):
    """Group-as-a-whole lifetime health score on channel_groups (mirrors the Channel
    health columns) - see app/health_score.py::apply_recording_health_observation."""
    existing = [r[1] for r in cur.execute('PRAGMA table_info(channel_groups)').fetchall()]
    if 'health_score' not in existing:
        cur.execute('ALTER TABLE channel_groups ADD COLUMN health_score FLOAT')
    if 'health_score_sample_count' not in existing:
        cur.execute('ALTER TABLE channel_groups ADD COLUMN health_score_sample_count '
                    'INTEGER NOT NULL DEFAULT 0')
    if 'health_score_updated_at' not in existing:
        cur.execute('ALTER TABLE channel_groups ADD COLUMN health_score_updated_at DATETIME')


def _m006_group_format_and_disable(conn, cur):
    """Channel-group format lock + member disable state (dev/changelog/159, 160):
    - channel_groups.format_resolution / format_fps: locked reference format (both NULL =
      auto-derived from best member; both set = pinned).
    - channel_groups.auto_disable_mismatched (default 1): the reconcile engine auto-disables
      format-mismatched members when on.
    - channels.group_disabled_reason: NULL=active, 'manual'=user-disabled, 'auto_mismatch'=
      engine-disabled for a format mismatch.
    No backfill - existing groups start auto-derived with auto-disable on, and the Part D
    startup sweep does the first reconcile (auto-disabling any pre-existing mismatches)."""
    cg_existing = [r[1] for r in cur.execute('PRAGMA table_info(channel_groups)').fetchall()]
    if 'format_resolution' not in cg_existing:
        cur.execute('ALTER TABLE channel_groups ADD COLUMN format_resolution VARCHAR(32)')
    if 'format_fps' not in cg_existing:
        cur.execute('ALTER TABLE channel_groups ADD COLUMN format_fps INTEGER')
    if 'auto_disable_mismatched' not in cg_existing:
        cur.execute('ALTER TABLE channel_groups ADD COLUMN auto_disable_mismatched '
                    'BOOLEAN NOT NULL DEFAULT 1')

    ch_existing = [r[1] for r in cur.execute('PRAGMA table_info(channels)').fetchall()]
    if 'group_disabled_reason' not in ch_existing:
        cur.execute('ALTER TABLE channels ADD COLUMN group_disabled_reason VARCHAR(32)')


def _m007_profile_retention(conn, cur):
    """recording_profiles.retention_days (nullable): per-profile override of the global
    recording.retention_days auto-delete window. NULL = use global; 0 = never delete."""
    existing = [r[1] for r in cur.execute('PRAGMA table_info(recording_profiles)').fetchall()]
    if 'retention_days' not in existing:
        cur.execute('ALTER TABLE recording_profiles ADD COLUMN retention_days INTEGER')


def _m008_segment_probe_columns(conn, cur):
    """recording_segments: capture-time stream-format columns (DESIGN.md section 5 -
    tech info must describe the original capture; probed by the watchdog once a
    segment has data). Guarded ADD COLUMN, idempotent."""
    existing = {row[1] for row in cur.execute('PRAGMA table_info(recording_segments)').fetchall()}
    for name, ddl in [
        ('probe_resolution',     'VARCHAR(32)'),
        ('probe_fps',            'FLOAT'),
        ('probe_audio_codec',    'VARCHAR(64)'),
        ('probe_audio_channels', 'INTEGER'),
        ('probed_at',            'DATETIME'),
    ]:
        if name not in existing:
            cur.execute(f'ALTER TABLE recording_segments ADD COLUMN {name} {ddl}')


def _m009_group_kind_columns(conn, cur):
    """Groups unification 1/4 (DESIGN-groups-unification.md), additive half only:
    channel_groups.kind ('channel' | 'check_only', constants in database.py) and
    is_system. Existing groups are all kind='channel'; is_system stays 0 until 3/4's
    migration creates the system check_only group. The channel_group_members join
    table itself is created by db.create_all() (new table). Deliberately NOT here:
    the membership backfill and the DROP of channels.group_id/group_disabled_reason -
    those ship in 2/4's step, in the same release as the read-site rewrite, so the
    legacy columns stay authoritative until the restart that ships converted code
    (sequencing decided 2026-07-20)."""
    existing = [r[1] for r in cur.execute('PRAGMA table_info(channel_groups)').fetchall()]
    if 'kind' not in existing:
        cur.execute("ALTER TABLE channel_groups ADD COLUMN kind VARCHAR(32) "
                    "NOT NULL DEFAULT 'channel'")
    if 'is_system' not in existing:
        cur.execute('ALTER TABLE channel_groups ADD COLUMN is_system BOOLEAN '
                    'NOT NULL DEFAULT 0')


def _m010_membership_backfill_and_drop(conn, cur):
    """Groups unification 2/4 (DESIGN-groups-unification.md): copy the legacy
    single-FK membership into channel_group_members, then DROP the two legacy
    channels columns. Ships in the same release as the membership-based read-site
    rewrite - after this step the join table is the only membership store.
    channel_group_members already exists at this point (db.create_all() runs before
    the migration runner). Position = id order within each group (arbitrary but
    stable; kind='channel' groups rank by score and ignore it). All raw SQL on the
    step's conn - no ORM. DROP COLUMN rehearsed against the exact live DDL in the
    1/4 session (passes on SQLite 3.45.1, indexes intact)."""
    existing = [r[1] for r in cur.execute('PRAGMA table_info(channels)').fetchall()]
    if 'group_id' in existing:
        reason_expr = ('group_disabled_reason' if 'group_disabled_reason' in existing
                       else 'NULL')
        cur.execute(f"""
            INSERT OR IGNORE INTO channel_group_members
                (group_id, channel_id, position, disabled_reason, created_at)
            SELECT group_id, id,
                   ROW_NUMBER() OVER (PARTITION BY group_id ORDER BY id) - 1,
                   {reason_expr}, CURRENT_TIMESTAMP
            FROM channels WHERE group_id IS NOT NULL
        """)
        cur.execute('ALTER TABLE channels DROP COLUMN group_id')
    if 'group_disabled_reason' in existing:
        cur.execute('ALTER TABLE channels DROP COLUMN group_disabled_reason')


def _m011_jobs_attach_to_groups(conn, cur):
    """Groups unification 3/4 (DESIGN-groups-unification.md): health-check jobs attach
    to a group instead of carrying their own channel list. Creates the pinned
    'TV Guide Channels' system check_only group (is_system=1, membership computed at
    run/display time - NO stored membership rows) and points the system job at it;
    every non-system job gets its own kind='check_only' group (name = job name) with
    membership rows in channel_ids_json order (position = list index) and
    disabled_channel_ids_json entries as disabled_reason='manual'; then DROPs both
    JSON columns. All raw SQL on the step's conn - no ORM.

    The 'check_only' / 'manual' literals and the columns holding them are era values with
    no counterpart in the current models: the unified model deleted kind, disabled_reason
    and auto_disable_mismatched, and step 41 refuses any database old enough to reach this
    step (dev/docs/DESIGN-channel-groups-model.md 4). It is kept spelled as it shipped so
    the ladder still describes what was actually done to a database of that vintage."""
    import json as _json
    existing = [r[1] for r in cur.execute('PRAGMA table_info(on_demand_test_jobs)').fetchall()]
    if 'group_id' not in existing:
        cur.execute('ALTER TABLE on_demand_test_jobs ADD COLUMN group_id INTEGER '
                    'REFERENCES channel_groups(id)')

    # Every NOT NULL channel_groups column is supplied explicitly - the model's
    # Python-side defaults don't exist as SQL DEFAULTs on columns from the original
    # create_all-era table (name, in_guide).
    _insert_group = ("INSERT INTO channel_groups (name, kind, is_system, in_guide, "
                     "guide_sort_order, auto_disable_mismatched, health_score_sample_count, "
                     "created_at, updated_at) "
                     "VALUES (?, 'check_only', ?, 0, 0, 1, 0, "
                     "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")

    row = cur.execute('SELECT id FROM channel_groups WHERE is_system=1').fetchone()
    if row is None:
        cur.execute(_insert_group, ('TV Guide Channels', 1))
        system_gid = cur.lastrowid
    else:
        system_gid = row[0]
    cur.execute('UPDATE on_demand_test_jobs SET group_id=? '
                'WHERE is_system=1 AND group_id IS NULL', (system_gid,))

    if 'channel_ids_json' in existing:
        jobs = cur.execute(
            'SELECT id, name, channel_ids_json, disabled_channel_ids_json '
            'FROM on_demand_test_jobs WHERE is_system=0 AND group_id IS NULL'
        ).fetchall()
        valid_ids = {r[0] for r in cur.execute('SELECT id FROM channels').fetchall()}
        for job_id, name, ids_json, disabled_json in jobs:
            try:
                ids = _json.loads(ids_json or '[]')
                disabled = set(_json.loads(disabled_json or '[]'))
            except ValueError:
                ids, disabled = [], set()
            cur.execute(_insert_group, (name, 0))
            gid = cur.lastrowid
            for pos, cid in enumerate(ids):
                if cid not in valid_ids:
                    continue
                cur.execute(
                    'INSERT OR IGNORE INTO channel_group_members '
                    '(group_id, channel_id, position, disabled_reason, created_at) '
                    'VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)',
                    (gid, cid, pos, 'manual' if cid in disabled else None))
            cur.execute('UPDATE on_demand_test_jobs SET group_id=? WHERE id=?',
                        (gid, job_id))
        cur.execute('ALTER TABLE on_demand_test_jobs DROP COLUMN channel_ids_json')
    if 'disabled_channel_ids_json' in existing:
        cur.execute('ALTER TABLE on_demand_test_jobs DROP COLUMN disabled_channel_ids_json')


def _m012_prerecord_checks(conn, cur):
    """Pre-recording health checks (DESIGN-prerecord-checks.md §3): additive columns for
    channel_tests.pre_check_recording_id (which recording a pre-check ran for) and
    recording_profiles.pre_check_enabled (nullable tri-state override, None = inherit the
    global channel_testing.pre_check.enabled flag). Guarded ADD COLUMN, idempotent."""
    existing = {row[1] for row in cur.execute('PRAGMA table_info(channel_tests)').fetchall()}
    if 'pre_check_recording_id' not in existing:
        cur.execute('ALTER TABLE channel_tests ADD COLUMN pre_check_recording_id INTEGER '
                    'REFERENCES recordings(id)')

    existing = {row[1] for row in cur.execute('PRAGMA table_info(recording_profiles)').fetchall()}
    if 'pre_check_enabled' not in existing:
        cur.execute('ALTER TABLE recording_profiles ADD COLUMN pre_check_enabled BOOLEAN')


def _m013_channel_lifecycle(conn, cur):
    """Channel lifecycle tracking (DESIGN-sync-resilience.md §5): first_seen_at (set once
    at row creation) / last_seen_at (updated on every sync whose feed includes the
    channel) - the raw data behind the derived "missing from provider" / "new" display
    states and the SYNC_FEED_SHRUNK / SYNC_CHANNELS_MISSING / SYNC_CHANNELS_NEW alerts.
    Backfilled from COALESCE(created_at, now) so existing rows get a sane starting value.
    Guarded ADD COLUMN, idempotent."""
    existing = {row[1] for row in cur.execute('PRAGMA table_info(channels)').fetchall()}
    if 'first_seen_at' not in existing:
        cur.execute('ALTER TABLE channels ADD COLUMN first_seen_at DATETIME')
    if 'last_seen_at' not in existing:
        cur.execute('ALTER TABLE channels ADD COLUMN last_seen_at DATETIME')

    cur.execute(
        "UPDATE channels SET first_seen_at = COALESCE(first_seen_at, created_at, CURRENT_TIMESTAMP) "
        "WHERE first_seen_at IS NULL"
    )
    cur.execute(
        "UPDATE channels SET last_seen_at = COALESCE(last_seen_at, created_at, CURRENT_TIMESTAMP) "
        "WHERE last_seen_at IS NULL"
    )
    cur.execute('CREATE INDEX IF NOT EXISTS ix_channels_last_seen_at ON channels (last_seen_at)')


def _m014_url_normalization_mode(conn, cur):
    """accounts.url_normalization: Boolean -> String mode (changelog/258 Spec §1).

    The old boolean's "enabled" had exactly one behavior - the MPEG-TS-without-live form -
    so True maps to 'mpegts' and an existing install keeps the URLs it already has. NULL
    stays NULL, meaning "use the global default".

    SQLite stores booleans as 1/0, and its dynamic typing lets the same column hold the new
    strings without a table rebuild, so this is an UPDATE rather than an ALTER. Written to
    be idempotent: rows already holding a mode string are left alone."""
    modes = ('disabled', 'mpegts', 'mpegts_live', 'hls')
    placeholders = ','.join('?' for _ in modes)
    cur.execute(
        f"UPDATE accounts SET url_normalization = 'mpegts' "
        f"WHERE url_normalization IN ('1', 1) "
        f"AND url_normalization NOT IN ({placeholders})", modes)
    cur.execute(
        f"UPDATE accounts SET url_normalization = 'disabled' "
        f"WHERE url_normalization IN ('0', 0) "
        f"AND url_normalization NOT IN ({placeholders})", modes)


def _m015_channel_test_quality_profile(conn, cur):
    """channel_tests: stream quality profile columns (DESIGN-stream-quality-profile.md §2).
    Informational-only capture of how a feed looks when it's up (codec/bit-depth/chroma/
    interlacing/coded-dims/CFR-VFR/bits-per-pixel-frame/timeline-gaps), separate from
    health_score. All nullable, populated on the next test - no backfill. Guarded ADD
    COLUMN, idempotent."""
    existing = {row[1] for row in cur.execute('PRAGMA table_info(channel_tests)').fetchall()}
    for name, ddl in [
        ('video_codec',          'VARCHAR(32)'),
        ('pix_fmt',              'VARCHAR(32)'),
        ('bit_depth',            'INTEGER'),
        ('chroma_subsampling',   'VARCHAR(8)'),
        ('interlaced',           'BOOLEAN'),
        ('coded_resolution',     'VARCHAR(32)'),
        ('is_vfr',               'BOOLEAN'),
        ('bits_per_pixel_frame', 'FLOAT'),
        ('timeline_gap_count',   'INTEGER'),
        ('timeline_gap_seconds', 'FLOAT'),
    ]:
        if name not in existing:
            cur.execute(f'ALTER TABLE channel_tests ADD COLUMN {name} {ddl}')


def _m016_conversion_progress(conn, cur):
    """recordings: supervised-conversion monitor state (dev/changelog/276).
    conversion_attempts + a persisted progress snapshot (pct/out_size/eta/started_at/
    updated_at) so a CONVERTING row can be monitored, auto-restarted, and rendered live on
    the detail strip and dashboard. All nullable, no backfill - populated on the next
    conversion. Guarded ADD COLUMN, idempotent."""
    existing = {row[1] for row in cur.execute('PRAGMA table_info(recordings)').fetchall()}
    for name, ddl in [
        ('conversion_attempts',     'INTEGER DEFAULT 0'),
        ('conversion_progress_pct', 'FLOAT'),
        ('conversion_out_size',     'INTEGER'),
        ('conversion_eta_seconds',  'INTEGER'),
        ('conversion_started_at',   'DATETIME'),
        ('conversion_updated_at',   'DATETIME'),
    ]:
        if name not in existing:
            cur.execute(f'ALTER TABLE recordings ADD COLUMN {name} {ddl}')


def _m017_epg_composite_index(conn, cur):
    """epg_entries: composite index on (channel_id, stop_time) for the per-account
    EPG-match correlated-EXISTS query (app/routes/accounts.py::_epg_match_counts;
    originally /channels/epg before that page's retirement, dev/changelog/631), which
    filters on both columns together and previously only had single-column indexes to
    scan the ~2M-row table. Measured 1.37s -> 0.34s on a scratch DB copy. A 3-column
    index adding start_time was tested and rejected (0.22s, marginal gain for a much
    larger index). Guarded CREATE INDEX IF NOT EXISTS, idempotent."""
    cur.execute(
        'CREATE INDEX IF NOT EXISTS ix_epg_entries_channel_stop '
        'ON epg_entries (channel_id, stop_time)'
    )


def _m018_account_sync_columns(conn, cur):
    """accounts: sync_interval_hours/sync_enabled/max_connections columns, folded in from
    the legacy app/scheduler.py::_migrate_db() (a different, pre-versioning ad-hoc migration
    than the one already folded into _m001_baseline above - that one lived in
    app/__init__.py). Guarded ADD COLUMN, idempotent."""
    existing = {row[1] for row in cur.execute('PRAGMA table_info(accounts)').fetchall()}
    for name, ddl in [
        ('sync_interval_hours', 'INTEGER'),
        ('sync_enabled',        'INTEGER NOT NULL DEFAULT 1'),
        ('max_connections',     'INTEGER'),
    ]:
        if name not in existing:
            cur.execute(f'ALTER TABLE accounts ADD COLUMN {name} {ddl}')


def _m019_account_constructed_url_count(conn, cur):
    """accounts: constructed_stream_url_count, cached count of channels whose stream URL
    was built by ChannelBin rather than supplied by the provider (backs the /accounts
    "Constructed URLs" badge). Guarded ADD COLUMN, idempotent."""
    existing = {row[1] for row in cur.execute('PRAGMA table_info(accounts)').fetchall()}
    if 'constructed_stream_url_count' not in existing:
        cur.execute('ALTER TABLE accounts ADD COLUMN constructed_stream_url_count INTEGER DEFAULT 0')


def _m020_recording_diagnostics_and_format(conn, cur):
    """recordings + recording_segments: seek-scan timeline stats and the stream format
    profile (dev/changelog/330). Five timeline_* columns on recordings hold what
    scan_video_timeline already measures and postprocessor.py discarded; the format columns
    hold what parse_ffprobe already returns and both recording-side callers discarded.
    Both tables get a profile because a re-encode changes codec and pixel format, so the
    output file's profile (recordings.recorded_*) is not necessarily the profile of what
    the provider sent (recording_segments.probe_*). All nullable, no backfill - populated
    on the next capture/post-process. Guarded ADD COLUMN, idempotent."""
    existing = {row[1] for row in cur.execute('PRAGMA table_info(recordings)').fetchall()}
    for name, ddl in [
        ('timeline_gap_count',           'INTEGER'),
        ('timeline_gap_seconds',         'FLOAT'),
        ('timeline_max_gap_seconds',     'FLOAT'),
        ('timeline_deficit_seconds',     'FLOAT'),
        ('timeline_damaged',             'BOOLEAN'),
        ('recorded_video_codec',         'VARCHAR(32)'),
        ('recorded_pix_fmt',             'VARCHAR(32)'),
        ('recorded_bit_depth',           'INTEGER'),
        ('recorded_chroma_subsampling',  'VARCHAR(8)'),
        ('recorded_interlaced',          'BOOLEAN'),
        ('recorded_coded_resolution',    'VARCHAR(32)'),
        ('recorded_is_vfr',              'BOOLEAN'),
        ('recorded_bits_per_pixel_frame', 'FLOAT'),
        ('recorded_audio_sample_rate',   'INTEGER'),
        ('recorded_audio_bitrate_kbps',  'FLOAT'),
        ('recorded_audio_language',      'VARCHAR(32)'),
    ]:
        if name not in existing:
            cur.execute(f'ALTER TABLE recordings ADD COLUMN {name} {ddl}')

    existing = {row[1] for row in cur.execute('PRAGMA table_info(recording_segments)').fetchall()}
    for name, ddl in [
        ('probe_video_codec',        'VARCHAR(32)'),
        ('probe_pix_fmt',            'VARCHAR(32)'),
        ('probe_bit_depth',          'INTEGER'),
        ('probe_chroma_subsampling', 'VARCHAR(8)'),
        ('probe_interlaced',         'BOOLEAN'),
        ('probe_coded_resolution',   'VARCHAR(32)'),
        ('probe_is_vfr',             'BOOLEAN'),
    ]:
        if name not in existing:
            cur.execute(f'ALTER TABLE recording_segments ADD COLUMN {name} {ddl}')


def _ensure_search_index_shape(conn, cur):
    """Bring the FTS index tables up to the shape app/search_index.py currently expects.

    Every migration below that rebuilds an index does so through the **current** shared SQL
    in search_index.py, not a copy pinned to its own era - that is what keeps one home for
    the rebuild. The cost of that choice is this function: a database migrating up from an
    older version reaches those steps with an older-shaped chan_prog, and the current rebuild
    would fail on a column it does not have. So each of them calls this first.

    Widening chan_prog is an ALTER; widening chan_prog_fts is not - an FTS5 virtual table's
    column set is fixed at creation, so it is dropped and recreated. Both are safe here
    because chan_prog is a derived cache with no source of truth in it: the rebuild that
    follows repopulates every row.
    """
    from .search_index import SEARCH_INDEX_DDL
    for stmt in SEARCH_INDEX_DDL:
        cur.execute(stmt)

    cols = [r[1] for r in cur.execute('PRAGMA table_info(chan_prog)').fetchall()]
    if 'description' not in cols:
        cur.execute('ALTER TABLE chan_prog ADD COLUMN description TEXT')

    fts_cols = [r[1] for r in cur.execute('PRAGMA table_info(chan_prog_fts)').fetchall()]
    if 'description' not in fts_cols:
        cur.execute('DROP TABLE chan_prog_fts')
        for stmt in SEARCH_INDEX_DDL:
            cur.execute(stmt)
    conn.commit()


def _m021_search_indexes(conn, cur):
    """FTS5 trigram search indexes: ch_fts over channels, chan_prog + chan_prog_fts over
    future EPG (dev/changelog/364). The DDL is shared with app/search_index.py rather than
    copied, because create_app() has to run the same statements on the fresh-database path -
    create_all() cannot build a virtual table and this runner skips every step on a fresh DB.

    The first build runs here so an existing database comes up already indexed instead of
    serving an empty index until its next sync. The raw half is committed before the ORM
    rebuild starts: db.session opens its own connection, which would deadlock against this
    step's open write transaction.
    """
    from .search_index import SEARCH_INDEX_NAMES, STATUS_OK, rebuild_through_cursor
    _ensure_search_index_shape(conn, cur)

    # Built on this cursor, not through rebuild_search_indexes(): that helper alerts on
    # failure, and alerts write Alert rows through the ORM while this transaction is open.
    # A failure here aborts startup with the snapshot intact, which is the louder outcome.
    for name in SEARCH_INDEX_NAMES:
        duration_ms, row_count, _watermark = rebuild_through_cursor(name, conn, cur)
        cur.execute(
            'INSERT INTO search_index_state (name, status, rebuilt_at, duration_ms, row_count) '
            'VALUES (?, ?, ?, ?, ?) '
            'ON CONFLICT(name) DO UPDATE SET status=excluded.status, '
            'rebuilt_at=excluded.rebuilt_at, duration_ms=excluded.duration_ms, '
            'row_count=excluded.row_count, error=NULL',
            (name, STATUS_OK, datetime.utcnow(), duration_ms, row_count),
        )
        conn.commit()
        log.info('Search index %r built in %d ms (%d rows)', name, duration_ms, row_count)


def _m022_search_index_watermark(conn, cur):
    """search_index_state.source_watermark + a rebuild that records it (dev/changelog/365).

    The column is what lets a readiness check detect an index whose rebuild never ran, which
    matters from this release on because the channel search actually reads the index now.

    The rebuild is not optional housekeeping. Backfilling the column from a watermark read
    *now* would assert that the existing index matches today's source, which is exactly the
    claim that cannot be verified after the fact - syncs have run since it was built. So the
    indexes are rebuilt here and the watermark recorded from that build, the same way m021
    did its first build, for the same reason: come up genuinely indexed rather than
    plausibly indexed.
    """
    from .search_index import SEARCH_INDEX_NAMES, STATUS_OK, rebuild_through_cursor
    existing = [r[1] for r in cur.execute('PRAGMA table_info(search_index_state)').fetchall()]
    if 'source_watermark' not in existing:
        cur.execute('ALTER TABLE search_index_state ADD COLUMN source_watermark TEXT')
    conn.commit()
    _ensure_search_index_shape(conn, cur)

    for name in SEARCH_INDEX_NAMES:
        duration_ms, row_count, watermark = rebuild_through_cursor(name, conn, cur)
        cur.execute(
            'INSERT INTO search_index_state '
            '(name, status, rebuilt_at, duration_ms, row_count, source_watermark) '
            'VALUES (?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(name) DO UPDATE SET status=excluded.status, '
            'rebuilt_at=excluded.rebuilt_at, duration_ms=excluded.duration_ms, '
            'row_count=excluded.row_count, source_watermark=excluded.source_watermark, '
            'error=NULL',
            (name, STATUS_OK, datetime.utcnow(), duration_ms, row_count, watermark),
        )
        conn.commit()
        log.info('Search index %r rebuilt in %d ms (%d rows, watermark %r)',
                 name, duration_ms, row_count, watermark)


def _m023_chan_prog_description(conn, cur):
    """chan_prog + chan_prog_fts widen to carry the program description.

    Descriptions are where this app's searches actually land - an EPG title is often just
    "Live Sport", with the teams named only in the description - so a channel search that
    cannot see them misses the results a user is actually looking for
    (dev/docs/DESIGN-search-indexes.md sections 3 and 4.1).

    This is a full rebuild of the programs index, not a backfill: description is part of the
    DISTINCT that produces chan_prog, so adding it changes which rows exist (+15% at
    production scale), not just what is in them. Expect roughly a minute on a database with a
    full EPG - ~27s for the DISTINCT scan, ~2s to repopulate, ~33s for the chunked FTS
    population. The channels index is untouched and deliberately not rebuilt.
    """
    from .search_index import (SEARCH_INDEX_PROGRAMS, STATUS_OK, rebuild_through_cursor)
    _ensure_search_index_shape(conn, cur)

    duration_ms, row_count, watermark = rebuild_through_cursor(SEARCH_INDEX_PROGRAMS, conn, cur)
    cur.execute(
        'INSERT INTO search_index_state '
        '(name, status, rebuilt_at, duration_ms, row_count, source_watermark) '
        'VALUES (?, ?, ?, ?, ?, ?) '
        'ON CONFLICT(name) DO UPDATE SET status=excluded.status, '
        'rebuilt_at=excluded.rebuilt_at, duration_ms=excluded.duration_ms, '
        'row_count=excluded.row_count, source_watermark=excluded.source_watermark, '
        'error=NULL',
        (SEARCH_INDEX_PROGRAMS, STATUS_OK, datetime.utcnow(), duration_ms, row_count,
         watermark),
    )
    conn.commit()
    log.info('Search index %r rebuilt with descriptions in %d ms (%d rows, watermark %r)',
             SEARCH_INDEX_PROGRAMS, duration_ms, row_count, watermark)


def _m024_channel_search_support(conn, cur):
    """channels: url_normalizable + the facet indexes the revamped channel search needs.

    Two independent pieces that ship together because both exist only to serve
    app/channel_search.py and neither is worth its own restart:

    **url_normalizable** backs the "Hide 'not normalized' URLs" standing option, which is on
    by default and therefore gates every query the search runs. It is
    accounts.url_is_normalizable(raw_stream_url) - a regex over the URL path - and there is
    no SQL spelling of it: deriving it per request meant fetching all 136,130 URLs and
    running the regex on each (~1.3s measured), the exact hidden-I/O shape CLAUDE.md
    forbids. Do NOT try to replace it with a LIKE/GLOB on stream_url; a bare numeric path is
    an Icecast radio mount, and URL-shape heuristics on this data have been measured wrong
    before (CLAUDE.md, the live-vs-VOD rule).

    ADD COLUMN with a constant default is metadata-only in SQLite, so the backfill only has
    to write the minority that is False (852 of 136,130 on this database).

    **The five indexes** are what dev/changelog/395's A5 measurement found actually moves the
    unfiltered facet pass, which is the default page load and costs ~295ms unindexed. The win
    is not a covering index (that was the hypothesis, and it was worth only 295 -> 238ms) but
    ordered single-column indexes, so each facet's GROUP BY / COUNT(*) FILTER is an index
    scan already in group order and needs no sort or hash: category 67ms -> 11.7ms, and the
    health + other pass 68ms -> 39.8ms. The account facet needs nothing - the
    (account_id, stream_id) unique constraint's autoindex already covers it.

    ix_channels_health carries manual_health_adjustment as a second column deliberately: the
    health band is the *effective* score (health_score + manual_health_adjustment, what
    _macros.html::health_score_badge shows), so an index on health_score alone would not
    cover the aggregate and SQLite would fall back to the table.
    """
    from .accounts import url_is_normalizable

    existing = {row[1] for row in cur.execute('PRAGMA table_info(channels)').fetchall()}
    added = 'url_normalizable' not in existing
    if added:
        _register_backfill(conn, cur, _BF_URL_NORMALIZABLE)
        cur.execute('ALTER TABLE channels ADD COLUMN url_normalizable '
                    'BOOLEAN NOT NULL DEFAULT 1')
        conn.commit()
    # Gated on the ledger, not on `added`: the writes below commit in chunks, so an
    # interrupted run leaves the column present and the flags partly written, and reading
    # the column's existence as "done" would freeze that half-written state permanently.
    # Re-running is safe - every flag is derived from the URL, not from a diff.
    if _backfill_needed(cur, _BF_URL_NORMALIZABLE, added):
        # raw_stream_url is what normalization actually reads; stream_url is the fallback
        # for rows synced before raw_stream_url existed, matching
        # routes/channels.py::_unnormalizable_channel_ids.
        rows = cur.execute('SELECT id, raw_stream_url, stream_url FROM channels').fetchall()
        bad = [r[0] for r in rows if not url_is_normalizable(r[1] or r[2] or '')]
        for lo in range(0, len(bad), 500):
            chunk = bad[lo:lo + 500]
            cur.execute('UPDATE channels SET url_normalizable = 0 WHERE id IN '
                        f'({",".join("?" * len(chunk))})', chunk)
            conn.commit()
        log.info('Backfill: %d of %d channels have no normalizable stream URL',
                 len(bad), len(rows))
        _finish_backfill(conn, cur, _BF_URL_NORMALIZABLE)

    for name, cols in [
        ('ix_channels_category_name',           'category_name'),
        ('ix_channels_in_guide',                'in_guide'),
        ('ix_channels_is_duplicate_stream_url', 'is_duplicate_stream_url'),
        ('ix_channels_test_enabled',            'test_enabled'),
        ('ix_channels_health',                  'health_score, manual_health_adjustment'),
    ]:
        cur.execute(f'CREATE INDEX IF NOT EXISTS {name} ON channels ({cols})')
    conn.commit()


def _m025_epg_start_stop_index(conn, cur):
    """epg_entries: composite (start_time, stop_time) for the airing grain's first page.

    The airing grain's default list is `WHERE stop_time > now ORDER BY start_time LIMIT n`,
    which is the query behind the very first paint of the Guide (EPG) tab. Without this index
    SQLite walks ix_epg_entries_start_time in start order and reads the TABLE row for each one
    to test stop_time, so it has to touch and discard every already-ended row before the first
    match - 746,003 of them on this database. With both columns in one index the same walk
    tests stop_time off the index and skips that prefix without a table read.

    Measured on the production database (1,418,920 EPG rows), warm, best of three:
    **1,716ms -> 127ms**, a 13.5x win. The index builds in 3.6s.

    TWO THINGS A FUTURE EDITOR SHOULD NOT RE-DERIVE, both measured 2026-07-31:

    * **stop_time was ALREADY indexed** and has been since the original schema
      (database.py's EPGEntry.stop_time carries index=True). A bare (stop_time) index was
      once proposed on the theory that it was missing, and the unfiltered list's 2-7s was
      blamed on that; the count cited as 1,242ms measures 42ms here. The index that really
      was missing is this composite. Do not add a bare (stop_time) index expecting a win -
      it exists.
    * **This does NOT make the unfiltered airing list fast**, and that is not a defect in this
      index. What is left is `_standing_breakdown`'s GROUP BY over the 1.4M-row
      epg_entries/channels join: ~900ms floor, plus 0.6-1.1s for each cluster-scoped standing
      option that is on. That is a caching question, not an indexing one - see
      DESIGN-channel-search.md 10's second deliberate lever - and it stays open.

    Full write-up: dev/changelog/414.
    """
    cur.execute('CREATE INDEX IF NOT EXISTS ix_epg_entries_start_stop '
                'ON epg_entries (start_time, stop_time)')
    conn.commit()


def _m026_channel_failure_streak(conn, cur):
    """channels: consecutive_test_failures column (dev/changelog/478) - the trailing count
    of consecutive FAILED ChannelTest rows (CANCELLED skipped, mirroring
    score_test_quality's own CANCELLED exclusion), maintained going forward by
    health_score.py::apply_test_health_observation. A distinct signal from health_score:
    its decay-weighted blend can leave a channel with a good prior history well above
    the failing band through weeks of hard test failures (measured: channel 9083
    sat at effective score 46.7 after 17 straight days FAILED).

    Backfilled from existing ChannelTest history below so an already-streaking channel
    is flagged the moment this migration runs, not only after a fresh run of future
    failures re-earns it."""
    existing = [r[1] for r in cur.execute('PRAGMA table_info(channels)').fetchall()]
    added = 'consecutive_test_failures' not in existing
    if added:
        _register_backfill(conn, cur, _BF_FAILURE_STREAK)
        cur.execute('ALTER TABLE channels ADD COLUMN consecutive_test_failures '
                    'INTEGER NOT NULL DEFAULT 0')
    # Same lock-avoidance rule as _m001_baseline: commit the raw-SQL half before the
    # ORM backfill below opens its own db.session connection. The backfill is gated on the
    # ledger rather than on `added`, so the gap between those two commits cannot swallow it.
    conn.commit()
    if _backfill_needed(cur, _BF_FAILURE_STREAK, added):
        _backfill_consecutive_test_failures()
        _finish_backfill(conn, cur, _BF_FAILURE_STREAK)


def _m027_sync_log_added_removed(conn, cur):
    """account_sync_logs: channels_added/channels_removed columns (dev/changelog/480) -
    per-sync added/removed breakdown for the account-activity view. NULL on rows synced
    before this migration - Product Principle 1: "not tracked", never a false zero. No
    backfill - a real historical value can't be reconstructed for old rows."""
    existing = [r[1] for r in cur.execute('PRAGMA table_info(account_sync_logs)').fetchall()]
    if 'channels_added' not in existing:
        cur.execute('ALTER TABLE account_sync_logs ADD COLUMN channels_added INTEGER')
    if 'channels_removed' not in existing:
        cur.execute('ALTER TABLE account_sync_logs ADD COLUMN channels_removed INTEGER')
    conn.commit()


def _m028_near_empty_and_capture_quality(conn, cur):
    """recordings/recording_segments: near-empty/slate segment detection rollup +
    capture-quality correction breakdown (dev/changelog/484). NULL on rows
    predating this migration - Product Principle 1: "not evaluated", never a false negative.
    No backfill - the underlying segment byte/span data still exists for old rows, but
    recomputing it retroactively is out of scope for an additive migration."""
    rec_cols = [r[1] for r in cur.execute('PRAGMA table_info(recordings)').fetchall()]
    if 'near_empty_segment_count' not in rec_cols:
        cur.execute('ALTER TABLE recordings ADD COLUMN near_empty_segment_count INTEGER')
    if 'near_empty_seconds' not in rec_cols:
        cur.execute('ALTER TABLE recordings ADD COLUMN near_empty_seconds FLOAT')
    if 'capture_quality_breakdown' not in rec_cols:
        cur.execute('ALTER TABLE recordings ADD COLUMN capture_quality_breakdown TEXT')

    seg_cols = [r[1] for r in cur.execute('PRAGMA table_info(recording_segments)').fetchall()]
    if 'near_empty' not in seg_cols:
        cur.execute('ALTER TABLE recording_segments ADD COLUMN near_empty BOOLEAN')
    conn.commit()


def _m029_check_window_columns(conn, cur):
    """on_demand_test_jobs: maintenance-window dispatch columns (app/check_window.py).
    recur_use_window opts a recurring check into the dispatcher instead of an exact
    recur_hour:recur_minute CronTrigger; last_full_run_at is the due_jobs() ordering key
    (set only on an untruncated finish); window_skip_until is "skip next run" for a job
    with no APScheduler job to modify_job. No backfill - all default False/NULL, meaning
    every existing recurring check keeps running at its exact time until explicitly
    switched into the window."""
    existing = [r[1] for r in cur.execute('PRAGMA table_info(on_demand_test_jobs)').fetchall()]
    if 'recur_use_window' not in existing:
        cur.execute('ALTER TABLE on_demand_test_jobs ADD COLUMN recur_use_window BOOLEAN NOT NULL DEFAULT 0')
        cur.execute('ALTER TABLE on_demand_test_jobs ADD COLUMN last_full_run_at DATETIME')
        cur.execute('ALTER TABLE on_demand_test_jobs ADD COLUMN window_skip_until DATETIME')
    conn.commit()


def _m030_group_clone_provenance(conn, cur):
    """channel_groups: cloned_from_group_id/cloned_from_name, a one-time provenance
    note stamped by clone_group() (app/routes/channel_groups.py) - "auto-select
    channels" on a health check, and the plain group Clone action, both go through
    it. Deliberately NOT a live pairing (see `paired`/attached_checks): the two
    groups are meant to diverge freely after the clone, same as the rest of that
    route's contract. No backfill - existing groups predate cloning-with-a-note and
    have no source to record."""
    existing = [r[1] for r in cur.execute('PRAGMA table_info(channel_groups)').fetchall()]
    if 'cloned_from_group_id' not in existing:
        cur.execute('ALTER TABLE channel_groups ADD COLUMN cloned_from_group_id INTEGER')
        cur.execute('ALTER TABLE channel_groups ADD COLUMN cloned_from_name VARCHAR(255)')
    conn.commit()


def _m031_account_provider_info(conn, cur):
    """accounts: capture what the Xtream auth response's user_info/server_info blocks
    carry and previously discarded (dev/changelog/534) - subscription expiry, trial
    status, the provider's own connection entitlement, allowed output formats, and its
    declared stream origin. All nullable, always None for M3U accounts (no such response),
    populated going forward only - no backfill, since older syncs never captured this.
    Guarded ADD COLUMN, idempotent."""
    existing = {row[1] for row in cur.execute('PRAGMA table_info(accounts)').fetchall()}
    for name, ddl in [
        ('provider_status',                 'VARCHAR(32)'),
        ('provider_exp_date',               'DATETIME'),
        ('provider_is_trial',               'BOOLEAN'),
        ('provider_max_connections',        'INTEGER'),
        ('provider_active_connections',     'INTEGER'),
        ('provider_allowed_output_formats', 'VARCHAR(255)'),
        ('provider_stream_origin',          'VARCHAR(255)'),
    ]:
        if name not in existing:
            cur.execute(f'ALTER TABLE accounts ADD COLUMN {name} {ddl}')


def _m032_segment_channel_id(conn, cur):
    """recording_segments: channel_id, so a channel-group recording's segments say which
    member channel actually captured them - Recording.channel_id only ever holds whichever
    member is CURRENT, which changes under failover (dev/changelog/536-adjacent work).
    Guarded ADD COLUMN, idempotent.

    Backfill: a non-group recording's segments all get its one recordings.channel_id -
    trivial, no correlation needed. A group recording is reconstructed from its own
    GROUP_MEMBER_SELECTED/GROUP_FAILOVER RecordingEvents (extra_data.channel_id /
    .to_channel_id), each carrying the timestamp its channel became active; every segment
    gets whichever transition's timestamp was the latest one at-or-before its own started_at.
    A group recording with no such events (edge case - predates the events, or the initial
    selection failed) falls back to recordings.channel_id, same as a non-group recording.
    """
    import json as _json

    existing = {row[1] for row in cur.execute('PRAGMA table_info(recording_segments)').fetchall()}
    added = 'channel_id' not in existing
    if added:
        _register_backfill(conn, cur, _BF_SEGMENT_CHANNEL)
        cur.execute('ALTER TABLE recording_segments ADD COLUMN channel_id INTEGER REFERENCES channels(id)')
        conn.commit()
    # The correlation below commits in stages, so returning early on "the column is there"
    # would leave a partially-correlated run frozen that way for good. Every segment's
    # channel is recomputed from the recording and its own events, so a re-run is a repeat
    # of the same answer rather than an increment on it.
    if not _backfill_needed(cur, _BF_SEGMENT_CHANNEL, added):
        return

    cur.execute(
        'UPDATE recording_segments SET channel_id = ('
        '  SELECT r.channel_id FROM recordings r WHERE r.id = recording_segments.recording_id'
        ') WHERE recording_id IN (SELECT id FROM recordings WHERE group_id IS NULL)'
    )
    conn.commit()

    group_recordings = cur.execute(
        'SELECT id, channel_id FROM recordings WHERE group_id IS NOT NULL'
    ).fetchall()
    for rec_id, rec_channel_id in group_recordings:
        events = cur.execute(
            "SELECT timestamp, extra_data, event_type FROM recording_events "
            "WHERE recording_id = ? AND event_type IN ('GROUP_MEMBER_SELECTED', 'GROUP_FAILOVER') "
            "ORDER BY timestamp, id", (rec_id,)
        ).fetchall()
        transitions = []  # (timestamp, channel_id), ascending
        for ts, extra_json, event_type in events:
            try:
                extra = _json.loads(extra_json or '{}')
            except ValueError:
                continue
            key = 'channel_id' if event_type == 'GROUP_MEMBER_SELECTED' else 'to_channel_id'
            cid = extra.get(key)
            if cid is not None:
                transitions.append((ts, cid))

        segments = cur.execute(
            'SELECT id, started_at FROM recording_segments WHERE recording_id = ? '
            'ORDER BY segment_number', (rec_id,)
        ).fetchall()
        for seg_id, started_at in segments:
            channel_id = rec_channel_id
            for ts, cid in transitions:
                if started_at is not None and ts <= started_at:
                    channel_id = cid
                else:
                    break
            cur.execute('UPDATE recording_segments SET channel_id = ? WHERE id = ?',
                        (channel_id, seg_id))
    conn.commit()
    _finish_backfill(conn, cur, _BF_SEGMENT_CHANNEL)


def _m033_account_xtream_debug_override(conn, cur):
    """accounts: xtream_debug_override, letting one account use the Fetch & Dump / Sync
    from dump troubleshooting tools without flipping the global debug.xtream_debug_mode
    flag on for every account (dev/changelog/547). Guarded ADD COLUMN, idempotent. No
    backfill - defaults false, same as a fresh column."""
    existing = {row[1] for row in cur.execute('PRAGMA table_info(accounts)').fetchall()}
    if 'xtream_debug_override' not in existing:
        cur.execute(
            'ALTER TABLE accounts ADD COLUMN xtream_debug_override BOOLEAN NOT NULL DEFAULT 0')


def _m034_channel_test_multi_track(conn, cur):
    """channel_tests: multi-track detection (dev/changelog/564). parse_ffprobe() now
    enumerates every stream instead of stopping at the first video/first audio match;
    video_codec/audio_codec etc still describe track 0 only, these three columns cover
    what's beyond that. video_track_count/audio_track_count are plain counts (sortable/
    filterable); extra_tracks is JSON - a per-track list can't be flattened into scalar
    columns, same shape as the existing quality_breakdown/blend_breakdown columns. All
    nullable, populated on the next test - no backfill. Guarded ADD COLUMN, idempotent."""
    existing = {row[1] for row in cur.execute('PRAGMA table_info(channel_tests)').fetchall()}
    for name, ddl in [
        ('video_track_count', 'INTEGER'),
        ('audio_track_count', 'INTEGER'),
        ('extra_tracks',      'TEXT'),
    ]:
        if name not in existing:
            cur.execute(f'ALTER TABLE channel_tests ADD COLUMN {name} {ddl}')


def _m035_dead_stream_retry(conn, cur):
    """recordings: dead-stream fast-fail retry budget (dead_stream_retry_count/next_retry_at)
    for the RETRYING status. Guarded ADD COLUMN, idempotent. No backfill - defaults to 0/NULL,
    same as a fresh column, and no pre-existing row could ever have been RETRYING."""
    existing = {row[1] for row in cur.execute('PRAGMA table_info(recordings)').fetchall()}
    if 'dead_stream_retry_count' not in existing:
        cur.execute(
            'ALTER TABLE recordings ADD COLUMN dead_stream_retry_count INTEGER NOT NULL DEFAULT 0')
    if 'next_retry_at' not in existing:
        cur.execute('ALTER TABLE recordings ADD COLUMN next_retry_at DATETIME')


def _m036_epg_duration_column(conn, cur):
    """epg_entries: duration_minutes, a VIRTUAL GENERATED column for the EPG Deep Search
    program-length filter (dev/changelog/594).

    Deliberately NOT a stored/app-maintained column. duration_minutes carries no information
    start_time/stop_time don't already have, so writing it from Python on every EPG upsert
    would be a second source of truth for one fact (the exact thing CLAUDE.md's Measurements
    rule warns against) with no benefit and a real drift risk. A VIRTUAL generated column is
    computed by SQLite itself from the two real columns, so it can never disagree with them,
    and the EPG sync path needs zero changes - it already writes only start_time/stop_time.

    Adding a VIRTUAL column to an existing table is schema-only (nothing is stored, so there
    is no table rewrite) - confirmed on a scratch copy of the production database (714,553
    rows): 0.05s. The index is what costs anything, and unlike a plain expression index, the
    query only has to spell a normal column name (`duration_minutes BETWEEN a AND b`) rather
    than repeat the generating expression verbatim for the planner to recognize it - one less
    place for a hand-copied expression to drift out of sync.

    Measured on that same scratch copy: an unindexed `duration_minutes BETWEEN 60 AND 240`
    scan is 11.26s; EXPLAIN QUERY PLAN confirms it is a full table scan. Indexed, the same
    query is 0.02s (562x) and the plan reads
    `SEARCH epg_entries USING INDEX ix_epg_entries_duration (duration_minutes>? AND
    duration_minutes<?)` - SQLite does use a b-tree index built on a generated column, which
    is not guaranteed by the SQL standard and is worth confirming empirically rather than
    assuming (dev/changelog/594's investigation for #16 in the same batch found two indexes
    that did NOT get picked up for a different query - this is not automatic). Index build
    took 1.25s at this row count.

    Guarded both ways (existing column, existing index via IF NOT EXISTS) so a re-run is a
    no-op, same as every other migration here.

    Uses `table_xinfo`, not the `table_info` every other migration's guard uses:
    **`PRAGMA table_info` never lists a generated column** (SQLite hides them there for
    compatibility with older readers - confirmed empirically, not documented behavior taken
    on faith). Guarding with `table_info` looks identical to every other additive migration
    and passes a first run clean, but the column is never seen as "already there," so a
    second run's ALTER TABLE hits `duplicate column name` instead of being the no-op every
    other migration in this file is. `table_xinfo` is the PRAGMA that includes generated/
    hidden columns.
    """
    existing = {row[1] for row in cur.execute('PRAGMA table_xinfo(epg_entries)').fetchall()}
    if 'duration_minutes' not in existing:
        cur.execute(
            'ALTER TABLE epg_entries ADD COLUMN duration_minutes INTEGER '
            'GENERATED ALWAYS AS '
            '(CAST((julianday(stop_time) - julianday(start_time)) * 1440 AS INTEGER)) VIRTUAL')
    cur.execute('CREATE INDEX IF NOT EXISTS ix_epg_entries_duration '
                'ON epg_entries (duration_minutes)')
    conn.commit()


def _m037_channel_logo_cache(conn, cur):
    """channels: logo_cache_path/logo_cache_source_url for local logo caching
    (app/logo_cache.py, dev/changelog/601). logo_cache_path is the cached file's name
    in the logos subfolder of recording.images_dir,
    NULL until the background job fetches it (or if the fetch failed/wasn't an image).
    logo_cache_source_url is the exact provider logo_url the cache was built from (or
    last attempted against) - comparing it to the current logo_url on each sync is the
    whole change-detection mechanism (refetch only when the URL string itself changes,
    no ETag/conditional-GET polling - deliberate, not a placeholder for something
    smarter). Both nullable, no backfill - a
    fresh column, same as a channel that has never been cached. Guarded ADD COLUMN,
    idempotent."""
    existing = {row[1] for row in cur.execute('PRAGMA table_info(channels)').fetchall()}
    if 'logo_cache_path' not in existing:
        cur.execute('ALTER TABLE channels ADD COLUMN logo_cache_path VARCHAR(255)')
    if 'logo_cache_source_url' not in existing:
        cur.execute('ALTER TABLE channels ADD COLUMN logo_cache_source_url VARCHAR(2048)')


def _m038_channel_search_text_watermark(conn, cur):
    """channels: search_text_updated_at, the stamp the channels search index's staleness
    watermark now reads (app/search_index.py::_SOURCE_WATERMARK_SQL, dev/changelog/674).
    Guarded ADD COLUMN + index, idempotent.

    **No backfill, deliberately.** NULL reads as "nothing this index covers has changed since
    the column existed", which is a perfectly good watermark - it only ever has to match
    itself - so there is no data half that a retry could skip. That keeps this step out of the
    interrupted-backfill class where the column-exists check passes on the retry and the rows
    are left half-populated forever.

    The last statement re-records the channels index's stored watermark in the new format.
    Without it every existing install reads stale the instant it starts - the stored string
    ends in a last_seen_at and the computed one now ends in an empty stamp - and pays a full
    rebuild plus a degraded search window for a format change that moved no data. It is
    applied ONLY where the stored watermark still equals what the OLD expression computes
    right now, i.e. where the index really is fresh; if it does not match, that index was
    already stale before this migration and the row is left alone so it still reads stale
    afterwards.

    Both watermark expressions are spelled out literally rather than imported from
    search_index.py. A migration has to keep working against the schema of its own era, and
    that dict is exactly what _m038 rewrites.
    """
    existing = {row[1] for row in cur.execute('PRAGMA table_info(channels)').fetchall()}
    if 'search_text_updated_at' not in existing:
        cur.execute('ALTER TABLE channels ADD COLUMN search_text_updated_at DATETIME')
    # Same name create_all() gives the model's index=True, so a fresh install and a migrated
    # one carry the identical index - the defect class _m024's five facet indexes fell into.
    cur.execute('CREATE INDEX IF NOT EXISTS ix_channels_search_text_updated_at '
                'ON channels (search_text_updated_at)')

    if not cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='search_index_state'").fetchone():
        return

    def _mark(*statements):
        parts = [cur.execute(sql).fetchone()[0] for sql in statements]
        return '/'.join('' if p is None else str(p) for p in parts)

    old = _mark('SELECT MAX(id) FROM channels', 'SELECT MAX(last_seen_at) FROM channels')
    new = _mark('SELECT MAX(id) FROM channels',
                'SELECT MAX(search_text_updated_at) FROM channels')
    cur.execute("UPDATE search_index_state SET source_watermark = ? "
                "WHERE name = 'channels' AND source_watermark = ?", (new, old))


def _m039_drop_prefix_redundant_indexes(conn, cur):
    """Drop six single-column indexes that are strict prefixes of a wider index or of a
    UNIQUE constraint on the same table, so a migrated database carries what a fresh
    create_all() now builds (dev/changelog/692).

    A b-tree on `(a)` can answer nothing a b-tree on `(a, b)` cannot, so none of these buys a
    read; each costs a write on every INSERT/UPDATE of its table. The reason they are being
    removed rather than tolerated is that a redundant prefix index is a DECOY: SQLite can
    prefer it over the wider one. `ix_epg_entries_start_time` was doing exactly that on the
    airing grain - it cannot answer stop_time from the index, so choosing it turned the
    default page from 0.091s into 1.222s, and the edit that tipped the choice was the removal
    of a duplicated ORDER BY term that could not change a single returned row.

    **ix_epg_entries_channel_id is deliberately NOT in this list**, though it is a strict
    prefix of ix_epg_entries_channel_stop. Measured, it is load-bearing: the airing grain's
    group-dedup subquery reads title/start_time/stop_time as well as channel_id, and with
    only the wider index available SQLite builds an AUTOMATIC PARTIAL COVERING INDEX over
    epg_entries on every query rather than using it - 0.098s to 2.914s. The static invariant
    carries it as a measured exemption; the rule is "measure each one", not "prefix indexes
    are always safe to drop".

    DROP INDEX IF EXISTS is idempotent and touches no row data, so this step is re-runnable
    and needs no backfill ledger entry.
    """
    for name in ('ix_epg_entries_start_time',
                 'ix_channels_account_id',
                 'ix_channel_events_channel_id',
                 'ix_channel_tests_channel_id',
                 'ix_channel_group_members_group_id',
                 'ix_ignored_alert_patterns_alert_type'):
        cur.execute(f'DROP INDEX IF EXISTS {name}')


def _m040_channel_lower_name_index(conn, cur):
    """channels: an expression index on (lower(name), id), the channel grain's default sort.

    The first EXPRESSION index in this schema, which is the part to be careful with: SQLite
    matches one by the TEXT of the expression, so `lower(name)` here and
    `func.lower(Channel.name)` in channel_search.py::SORTS are load-bearingly the same
    spelling. `name COLLATE NOCASE` would build a perfectly good index that the planner would
    never once choose.

    Why it earns its 10.1 MB: without it the default landing page sorts every surviving row
    into a temp B-tree before returning the first 100 - 105.5ms against 9.0ms on this
    machine's 138,415 channels. The write side is free in the case that actually repeats,
    because `last_seen_at` is not in the index and a steady-state re-sync touches nothing
    else (dev/changelog/699 has the full before/after, including the ~20s this step itself
    costs once on a database that size).

    CREATE INDEX IF NOT EXISTS is idempotent and moves no row data, so this step is
    re-runnable from the top and needs no backfill-ledger entry (dev/changelog/686).
    """
    cur.execute('CREATE INDEX IF NOT EXISTS ix_channels_lower_name '
                'ON channels (lower(name), id)')


def _m041_channel_group_model_requires_fresh_db(conn, cur):
    """channel groups: the unified model, which has no upgrade path from an older database.

    The rebuild deletes channel_groups.kind, channel_groups.auto_disable_mismatched and
    channel_group_members.disabled_reason, and adds the two participation columns, the
    format strategy and the channel_group_events table
    (dev/docs/DESIGN-channel-groups-model.md 4). A full wipe was approved instead of a
    migration, deliberately: the columns convert mechanically but the facts they encoded
    do not. Which members a user would record from, and which format a group should be,
    are answers this model wants from measured health data and from the user - inventing
    them from an old status enum would produce exactly the confidently-wrong numbers this
    app exists to refuse.

    So this step migrates nothing and refuses instead. It exists because the alternative
    is worse: without it two different schemas both stamp version 40, an older database
    starts up against code whose models no longer match it, and the first symptom is an
    OperationalError somewhere far from the cause. A refusal that names the problem and
    the fix is the honest failure.
    """
    raise SystemExit(
        'This build of ChannelBin rebuilt the channel group model and cannot upgrade an '
        'existing database (dev/docs/DESIGN-channel-groups-model.md). Start fresh: stop '
        'the app, move dvr.db together with its -wal and -shm files to a backup location '
        '(move, do not delete - it is the only copy of your channel test history), then '
        'start the app and re-add your accounts and groups.'
    )


def _m042_group_muted_warnings(conn, cur):
    """channel_groups: muted_warnings, the per-group set of hidden warning banners.

    A JSON list of database.GROUP_WARNING_KINDS values, NULL when nothing is hidden
    (DESIGN-channel-groups-model.md 16.2, dev/changelog/756). Nullable with no default and
    no backfill: an existing group has hidden nothing, which is exactly what NULL means
    here, so there is no obligation to register and nothing for a resumed run to redo
    (dev/changelog/686).

    ADD COLUMN guarded on PRAGMA table_info is idempotent, so this step is re-runnable
    from the top."""
    existing = {row[1] for row in cur.execute('PRAGMA table_info(channel_groups)').fetchall()}
    if 'muted_warnings' not in existing:
        cur.execute('ALTER TABLE channel_groups ADD COLUMN muted_warnings TEXT')


def _m043_channel_hidden_state(conn, cur):
    """channels: the four hiding columns plus the index every read site filters on.

    `hidden` is the materialized effective answer (app/channel_hiding.py), `hidden_override`
    the human's own force-hide/force-show, `hidden_reason` which source produced the answer,
    and `hidden_deferred` "a source said hide but guide/group membership is keeping this
    visible" (dev/changelog/775).

    No backfill and no obligation to register (dev/changelog/686): an existing channel has
    nobody's hide answer on it, which is exactly what the NOT NULL defaults and a NULL
    override already say. `hidden` therefore starts correct rather than starting wrong and
    waiting for a pass that a crash could skip.

    ADD COLUMN guarded on PRAGMA table_info and CREATE INDEX IF NOT EXISTS are both
    idempotent, so this step is re-runnable from the top."""
    existing = {row[1] for row in cur.execute('PRAGMA table_info(channels)').fetchall()}
    for name, decl in (('hidden', 'BOOLEAN NOT NULL DEFAULT 0'),
                       ('hidden_override', 'BOOLEAN'),
                       ('hidden_reason', 'VARCHAR(32)'),
                       ('hidden_deferred', 'BOOLEAN NOT NULL DEFAULT 0')):
        if name not in existing:
            cur.execute(f'ALTER TABLE channels ADD COLUMN {name} {decl}')
    cur.execute('CREATE INDEX IF NOT EXISTS ix_channels_hidden ON channels (hidden)')


def _m044_channel_hide_rules(conn, cur):
    """channel_hide_rules: the blanket GLOB rules over category and channel name.

    Sources 1-3 of the four that stack into `Channel.hidden` (app/channel_hiding.py,
    dev/docs/DESIGN-channel-hiding.md, dev/changelog/776). `account_id` NULL means global.

    No backfill and no obligation to register (dev/changelog/686): a database upgrading to
    this version has no rules, so every channel's existing `hidden` answer - computed from
    the human's override and guide/group protection alone - is already the correct answer
    for an empty rule set. The first rule saved recomputes the table.

    The two UNIQUE indexes are partial on purpose: SQLite treats NULLs as distinct, so a
    single UNIQUE over (account_id, target, pattern) would not stop two identical global
    rules. Splitting on `account_id IS NULL` covers both scopes.

    CREATE TABLE/INDEX IF NOT EXISTS throughout, so this step is re-runnable from the top."""
    cur.execute('''
        CREATE TABLE IF NOT EXISTS channel_hide_rules (
            id             INTEGER PRIMARY KEY,
            account_id     INTEGER REFERENCES accounts(id),
            target         VARCHAR(32) NOT NULL,
            pattern        VARCHAR(512) NOT NULL,
            enabled        BOOLEAN NOT NULL DEFAULT 1,
            created_at     DATETIME NOT NULL,
            updated_at     DATETIME,
            match_count    INTEGER,
            deferred_count INTEGER,
            counted_at     DATETIME
        )
    ''')
    cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_hide_rule_scoped '
                'ON channel_hide_rules (account_id, target, pattern) '
                'WHERE account_id IS NOT NULL')
    cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_hide_rule_global '
                'ON channel_hide_rules (target, pattern) WHERE account_id IS NULL')


def _m045_drop_channels_hidden_index(conn, cur):
    """Drop `ix_channels_hidden` and `ix_channels_test_enabled` - two indexes no query uses.

    Neither buys a read. Every SQL predicate on `channels.hidden` asks for the COMMON value
    (`hidden = 0`, "the channels you are offered"), which no index can answer usefully, and
    nothing filters `test_enabled` in SQL at all - every read of that column is Python-side
    over rows already loaded. What they cost is a write on each of 137,144 rows on every
    channel upsert and every hide-rule materialize.

    They are also a latent planner hazard rather than inert. On the isolated shape
    `WHERE hidden=0 ORDER BY lower(name), id LIMIT 100` SQLite prefers a boolean index over
    `ix_channels_lower_name`, cannot then answer the ORDER BY from it, and adds a temp b-tree
    over every row: 135.1 ms against 0.2 ms, measured on the production database with only
    the index choice differing. The channel search's real default query is more predicated
    than that and was measured UNAFFECTED either way (~185 ms with and without), so this
    removes a hazard and a write cost - it is not a speedup, and dev/changelog/781 says so
    rather than claiming one.

    The two boolean-led indexes that DO earn their place are kept, and the difference is
    which value their queries seek: ix_channels_in_guide (6 rows of 137,144 - 0.0 ms against
    171.4 ms without) and ix_channels_is_duplicate_stream_url (1,572 rows - 1.0 ms against
    40.5 ms) both seek the RARE value.

    DROP INDEX IF EXISTS is idempotent, so this step is re-runnable from the top."""
    cur.execute('DROP INDEX IF EXISTS ix_channels_hidden')
    cur.execute('DROP INDEX IF EXISTS ix_channels_test_enabled')


def _m046_account_hidden_channel_count(conn, cur):
    """accounts: hidden_channel_count, how many of this account's channels are currently
    hidden (app/channel_hiding.py, dev/docs/DESIGN-channel-hiding.md). Guarded ADD COLUMN.

    The backfill below is unconditional rather than gated behind the column-just-added
    check above it - deliberately, and without needing the backfill ledger further up this
    file. It is a full recompute (a fresh COUNT, not an increment onto a stored value), so
    running it again on a retried migration is always correct; the ledger exists for
    backfills where that would NOT be true (CLAUDE.md "already done is a fact you
    recorded")."""
    existing = {row[1] for row in cur.execute('PRAGMA table_info(accounts)').fetchall()}
    if 'hidden_channel_count' not in existing:
        cur.execute('ALTER TABLE accounts ADD COLUMN hidden_channel_count INTEGER DEFAULT 0')
    cur.execute('''
        UPDATE accounts SET hidden_channel_count = (
            SELECT COUNT(*) FROM channels
            WHERE channels.account_id = accounts.id AND channels.hidden = 1
        )
    ''')


def _m047_drop_channel_scoped_format_events(conn, cur):
    """Delete the CHANNEL_GROUP_FORMAT_* ChannelEvent rows and every GROUP_FORMAT_MISMATCH
    alert - the wreckage of the per-channel format state, plus a clean slate for the
    per-membership one that replaces it (dev/changelog/789).

    These rows cannot be migrated, and that is the defect rather than a shortcoming of the
    migration: `channel_events` has no group_id, so a row saying "this member's format
    differs from the group's" does not record WHICH group said so. On the database this
    was written against, six channels shared by two groups had accumulated 454 such events
    and 214 undismissed duplicate alerts, all of one flap.

    Deleting loses nothing recoverable. The reconcile engine re-derives the true state per
    group on its next pass - one accurate event and one accurate alert per genuinely
    mismatched membership - so a real mismatch is re-announced within a health check cycle
    rather than lost, and a stale one simply does not come back.

    Re-runnable from the top: a DELETE by predicate is idempotent, so a retry after an
    interrupted run deletes whatever the first attempt did not and needs no entry in the
    backfill ledger above."""
    cur.execute("""
        DELETE FROM channel_events
        WHERE event_type IN ('CHANNEL_GROUP_FORMAT_MISMATCH', 'CHANNEL_GROUP_FORMAT_RESOLVED')
    """)
    events = cur.rowcount
    cur.execute("DELETE FROM alerts WHERE alert_type = 'GROUP_FORMAT_MISMATCH'")
    alerts = cur.rowcount
    if events or alerts:
        log.info('Dropped %d channel-scoped format events and %d format-mismatch alerts; '
                 'the reconcile engine re-derives both per group', events, alerts)


def _m048_channel_standing_index(conn, cur):
    """channels: ix_channels_standing, so the standing breakdown stops scanning the table.

    `channel_search.py::_standing_breakdown_compute()` buckets every channel through one
    ordered CASE and counts the buckets. It reads only narrow columns, but it reads them off
    all 137,283 rows of a 63 MB table, so the SCAN is the cost - not the predicates, which
    were each measured on 2026-07-30 and are already the cheap spelling. That one statement
    is the largest single cost in three of the cells `dev/tools/search_timing_check.py`
    watches, `channels counts no-q` and `channels no-q rows` among them.

    Measured on a copy of the production database, only the index differing: the statement
    goes 181.9 ms -> 99.7 ms (the plan flips to SCAN channels USING COVERING INDEX), and end
    to end over HTTP `channels counts no-q` goes 219.5 ms -> 120.7 ms and `channels no-q
    rows` 268.8 ms -> 168.8 ms. The index is 1.80 MB (dev/changelog/834).

    This is boolean-led, which migration 45 dropped two indexes for being - and the
    difference is that neither of those was ever going to be read the way this one is.
    A boolean index is a decoy when a query SEEKS the common value; this one is never sought
    at all, it is scanned end to end because the CASE has to visit every row anyway. The
    decoy hazard was checked rather than argued: with this index present the default channel
    query, the one migration 45 was worried about, got faster and kept
    `ix_channels_lower_name`.

    CREATE INDEX IF NOT EXISTS is idempotent, so this step is re-runnable from the top."""
    cur.execute('CREATE INDEX IF NOT EXISTS ix_channels_standing '
                'ON channels (hidden, in_guide, url_normalizable, account_id, health_score)')


def _m049_profile_stall_move_trigger(conn, cur):
    """recording_profiles: stall_move_count/stall_move_window_minutes, the per-profile
    override of the stall-rate demotion trigger (dev/changelog/889), same nullable
    "None = use the global watchdog.* value" convention as the three columns beside them.

    Guarded ADD COLUMN, idempotent. No backfill and no obligation to register
    (dev/changelog/686): NULL is exactly what an existing profile should mean here - it
    inherits the global trigger, which is what it did before the columns existed."""
    existing = {row[1] for row in cur.execute(
        'PRAGMA table_info(recording_profiles)').fetchall()}
    if 'stall_move_count' not in existing:
        cur.execute('ALTER TABLE recording_profiles ADD COLUMN stall_move_count INTEGER')
    if 'stall_move_window_minutes' not in existing:
        cur.execute(
            'ALTER TABLE recording_profiles ADD COLUMN stall_move_window_minutes INTEGER')


def _m050_channel_health_exclusions(conn, cur):
    """channel_health_exclusions: the observations a user has taken out of a channel's
    health score, by hand (app/health_recompute.py, dev/changelog/895).

    No backfill and no obligation to register (dev/changelog/686): an upgrading database has
    no exclusions, and "every observation counts" is exactly what its stored scores already
    mean. The first reset or step-back writes the first rows.

    The UNIQUE index is what stops one observation being excluded twice, which would make
    the step-back count disagree with the replay it drives.

    CREATE TABLE/INDEX IF NOT EXISTS throughout, so this step is re-runnable from the top."""
    cur.execute('''
        CREATE TABLE IF NOT EXISTS channel_health_exclusions (
            id          INTEGER PRIMARY KEY,
            channel_id  INTEGER NOT NULL REFERENCES channels(id),
            source_kind VARCHAR(32) NOT NULL,
            source_id   INTEGER NOT NULL,
            excluded_at DATETIME NOT NULL,
            action      VARCHAR(16) NOT NULL
        )
    ''')
    cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_health_exclusion '
                'ON channel_health_exclusions (channel_id, source_kind, source_id)')


# The titles app/accounts.py::_do_sync gives these two alerts, which were the counts' only
# record before m051. They name the account rather than its id.
_SKIP_ALERT_TITLE = re.compile(
    r'^(?P<name>.*): skipped (?P<count>\d+) (?:malformed channel URL|duplicate stream ID)\(s\)$')
_SKIP_ALERT_COLUMN = {
    'MALFORMED_CHANNEL_URLS': 'skipped_malformed_urls',
    'DUPLICATE_STREAM_IDS_SKIPPED': 'skipped_duplicate_stream_ids',
}


def _m051_sync_log_skip_counts(conn, cur):
    """account_sync_logs: skipped_malformed_urls/skipped_duplicate_stream_ids, the entries each
    sync's channel upsert skipped, so the account page can show them (dev/changelog/926).

    Backfilled from the alerts through the ledger, because the columns existing is not
    evidence the backfill ran (dev/changelog/686)."""
    existing = {r[1] for r in cur.execute('PRAGMA table_info(account_sync_logs)').fetchall()}
    added = any(col not in existing for col in _SKIP_ALERT_COLUMN.values())
    if added:
        _register_backfill(conn, cur, _BF_SYNC_LOG_SKIP_COUNTS)
    for col in _SKIP_ALERT_COLUMN.values():
        if col not in existing:
            cur.execute(f'ALTER TABLE account_sync_logs ADD COLUMN {col} INTEGER')
    conn.commit()
    if _backfill_needed(cur, _BF_SYNC_LOG_SKIP_COUNTS, added):
        _backfill_sync_log_skip_counts(cur)
        _finish_backfill(conn, cur, _BF_SYNC_LOG_SKIP_COUNTS)


def _backfill_sync_log_skip_counts(cur):
    """Set each sync's counts from the alert it raised. A recompute - every matched column is
    set to the count in its alert's title - so a re-run after an interrupted attempt writes
    the same values. _finish_backfill commits it together with the ledger row.

    The alert fires milliseconds after its sync is stamped complete, so it is matched to its
    account's finished sync nearest in time, within a minute. Anything unmatched stays NULL,
    "not tracked": no alert does not prove nothing was skipped, since an ignored alert writes
    no row and dismissed alerts are pruned after alerts.keep_days. A name two accounts share
    matches neither, and a renamed account's older alerts match nothing."""
    ids_by_name = {}
    for account_id, name in cur.execute('SELECT id, name FROM accounts').fetchall():
        ids_by_name.setdefault(name, []).append(account_id)
    alerts = cur.execute(
        'SELECT alert_type, title, created_at FROM alerts WHERE alert_type IN (?, ?)',
        tuple(_SKIP_ALERT_COLUMN)).fetchall()
    for alert_type, title, created_at in alerts:
        match = _SKIP_ALERT_TITLE.match(title or '')
        account_ids = ids_by_name.get(match.group('name'), []) if match else []
        if len(account_ids) != 1:
            continue
        sync = cur.execute(
            "SELECT id FROM account_sync_logs WHERE account_id = ? "
            "AND status IN ('SUCCESS', 'PARTIAL') "
            "AND abs(julianday(completed_at) - julianday(?)) * 86400 <= 60 "
            "ORDER BY abs(julianday(completed_at) - julianday(?)) LIMIT 1",
            (account_ids[0], created_at, created_at)).fetchone()
        if sync:
            cur.execute(f'UPDATE account_sync_logs SET {_SKIP_ALERT_COLUMN[alert_type]} = ? '
                        'WHERE id = ?', (int(match.group('count')), sync[0]))


def _m052_dismiss_retired_alert_type_rows(conn, cur):
    """Dismiss every open alert of a type nothing raises any more (dev/changelog/928).

    Twelve types stopped being raised when alerts were narrowed to real problems: each
    reported something already shown on the group, recording, account or page it concerns.
    The rows are kept and still render with their label - the history is real - but left
    open they would hold the counter at a number no future event can ever bring down, since
    the code that used to dismiss them is gone too. On the database this was written
    against, all 124 unread WARN alerts were GROUP_FORMAT_MISMATCH.

    Dismissed rather than deleted, and deliberately not marked read: dismissing is the
    action the user would have taken, it is what every other resolution path does, and
    alerts.keep_days prunes dismissed rows on its own schedule. Marking them read would
    claim somebody looked at them.

    Re-runnable from the top: an UPDATE by predicate over the rows still open is idempotent
    - a retry after an interrupted run dismisses whatever the first attempt did not, and
    re-dismisses nothing - so this needs no entry in the backfill ledger above.

    The twelve names are spelled out here rather than read from alerts.RETIRED_ALERT_TYPES
    on purpose. A migration's effect must be what it was when it shipped: if a thirteenth
    type is retired later, this step must not silently start dismissing it as well - that
    belongs to a new migration, whose absence would otherwise be invisible.
    """
    types = [
        'CHANNEL_HIDE_RULES_NOT_APPLIED',
        'DUPLICATE_STREAM_IDS_SKIPPED',
        'GROUP_FORMAT_MISMATCH',
        'GROUP_NO_ELIGIBLE_MEMBER',
        'HEALTH_CHECK_COMPLETE',
        'JOB_SKIPPED',
        'MALFORMED_CHANNEL_URLS',
        'RECORDING_FORMAT_CHANGED',
        'RECORDING_FORMAT_OVERRIDE',
        'SYNC_CHANNELS_MISSING',
        'SYNC_CHANNELS_NEW',
        'SYNC_STREAM_URLS_CONSTRUCTED',
    ]
    placeholders = ','.join('?' * len(types))
    cur.execute(
        'UPDATE alerts SET dismissed_at = ? '
        f'WHERE dismissed_at IS NULL AND alert_type IN ({placeholders})',
        [_utc_stamp(), *types])
    if cur.rowcount:
        log.info('Dismissed %d open alert(s) of types nothing raises any more', cur.rowcount)


def _m053_unlink_stale_recording_references(conn, cur):
    """Unlink the rows still naming a recording they cannot be about (dev/changelog/929).

    recordings.id is a plain INTEGER PRIMARY KEY with no AUTOINCREMENT, so SQLite re-issues
    a deleted row's number to the next recording created. The delete paths used to leave
    alerts - and a pre-check's channel_tests rows - holding that number, so they did not
    dangle: they silently re-attached to whichever unrelated recording inherited it. On the
    database this was written against, two open "move failed" alerts about test recordings
    deleted the day before deep-linked to the two recordings scheduled the next morning
    under the same two ids.

    A row is stale when the recording it names is gone, OR when it predates that
    recording's own created_at - a fact about a recording cannot be older than the
    recording. The second half is what repairs the live rows: their ids exist again, so a
    missing-row check alone would have found nothing at all.

    Open alerts are dismissed as well as unlinked, and an already-dismissed row keeps its
    original timestamp. Re-runnable from the top: nulling the column is what takes a
    repaired row out of the predicate, so a retry after an interrupted run reaches only
    what the first attempt did not.
    """
    cur.execute(
        'UPDATE alerts SET dismissed_at = COALESCE(dismissed_at, ?), recording_id = NULL '
        'WHERE recording_id IS NOT NULL AND ('
        '  recording_id NOT IN (SELECT id FROM recordings)'
        '  OR created_at < (SELECT r.created_at FROM recordings r '
        '                   WHERE r.id = alerts.recording_id))',
        (_utc_stamp(),))
    if cur.rowcount:
        log.info('Unlinked %d alert(s) from a recording they cannot be about', cur.rowcount)

    cur.execute(
        'UPDATE channel_tests SET pre_check_recording_id = NULL '
        'WHERE pre_check_recording_id IS NOT NULL AND ('
        '  pre_check_recording_id NOT IN (SELECT id FROM recordings)'
        '  OR test_started_at < (SELECT r.created_at FROM recordings r '
        '                        WHERE r.id = channel_tests.pre_check_recording_id))')
    if cur.rowcount:
        log.info('Unlinked %d pre-check test(s) from a recording they cannot be about',
                 cur.rowcount)


def _m054_drop_guide_check_retarget_announcement(conn, cur):
    """Remove the one-time TV Guide retarget announcement's leftovers (dev/changelog/931).

    dev/changelog/752 changed what the automatic "TV Guide Channels" health check probes,
    and a startup hook announced that change once - an INFO alert, a group timeline event
    and a log line - gated on a user_prefs row so it could never fire twice. The hook is
    gone: it could only ever fire on an install upgrading across that one version boundary,
    because a database with no groups of the user's own resolves the old and new rules
    identically and stays silent, which is the shape of every fresh install. What it left
    behind is an alert nothing will ever dismiss and a flag guarding a code path that no
    longer exists.

    Deleted rather than dismissed, unlike the retired types of migration 52. Those keep
    their rows because the conditions they described were real and recurring, so the
    history is worth reading. This one announced a single upgrade to the person who
    performed it; once read, it is a release note sitting in the alert list.

    The ChannelGroupEvent it wrote is deliberately NOT removed - a group's Activity
    Timeline is where "what this check tests changed, on this date" belongs, and it costs
    nothing. GROUP_CHECK_TARGETS_CHANGED keeps its label so that row still renders.

    Re-runnable from the top: both statements are deletes by predicate, so a retry after an
    interrupted run removes whatever the first attempt did not, and nothing else.
    """
    cur.execute("DELETE FROM alerts WHERE alert_type = 'HEALTH_CHECK_TARGETS_CHANGED'")
    if cur.rowcount:
        log.info('Removed %d one-time health check retarget alert(s)', cur.rowcount)
    cur.execute("DELETE FROM user_prefs WHERE key = 'guide_check_retarget_announced'")
    if cur.rowcount:
        log.info('Removed the health check retarget announcement flag')


#: The table-constraint primary key SQLAlchemy emits for `recordings` without
#: sqlite_autoincrement, and the inline form it emits with it. AUTOINCREMENT is only legal on
#: the inline spelling, so the rebuild below has to move the clause, not just append a keyword.
_PK_TABLE_CONSTRAINT = '\n\tPRIMARY KEY (id), '
_PK_COLUMN_OLD = 'id INTEGER NOT NULL,'
_PK_COLUMN_NEW = 'id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,'


def _recordings_autoincrement_ddl(old_sql: str, table_name: str) -> str:
    """Rewrite `recordings`' CREATE TABLE so its primary key carries AUTOINCREMENT.

    Derived from the DDL actually stored in sqlite_master rather than written out here, so
    the rebuilt table is the source table's own vintage down to the column list, types and
    defaults - nothing to drift out of step with the model, and nothing for a later step that
    adds a column to collide with. The only difference is where the primary key is declared.

    Raises if either edit finds nothing. A rebuild that quietly produced a table without
    AUTOINCREMENT would leave the id still being re-issued while every check for the column's
    presence passed, which is the silent half-success this app refuses on principle.
    """
    new_sql = old_sql.replace('CREATE TABLE recordings', f'CREATE TABLE {table_name}', 1)
    if _PK_COLUMN_OLD not in new_sql or _PK_TABLE_CONSTRAINT not in new_sql:
        raise RuntimeError(
            'Cannot rebuild the recordings table with AUTOINCREMENT: its stored CREATE TABLE '
            'does not carry the expected primary key clauses '
            f'({_PK_COLUMN_OLD!r} and {_PK_TABLE_CONSTRAINT.strip()!r}). Stored DDL:\n{old_sql}')
    new_sql = new_sql.replace(_PK_COLUMN_OLD, _PK_COLUMN_NEW, 1)
    return new_sql.replace(_PK_TABLE_CONSTRAINT, '', 1)


def _m055_recordings_autoincrement(conn, cur):
    """Rebuild `recordings` with AUTOINCREMENT so a deleted id is never re-issued.

    SQLite issues max(id)+1 for a plain INTEGER PRIMARY KEY, so deleting the newest recording
    and creating another hands the new one the old one's number. Anything still holding that
    number does not dangle - it silently re-attaches and describes the wrong recording. Two
    open alerts about deleted test recordings deep-linked to the recordings that inherited
    their ids (dev/changelog/929, which unlinked them, and migration 53, which repaired the
    rows already stored). This closes the reuse itself, for every consumer of a recording id
    that has not been written yet.

    SQLite cannot add AUTOINCREMENT in place, so this is the documented copy/drop/rename
    rebuild. Three things about it are load-bearing:

    * **The explicit BEGIN.** Python's sqlite3 driver opens a transaction for INSERT but not
      for DDL, so without this the CREATE of the temporary table commits on its own. An
      interrupted rebuild then leaves recordings_autoinc_new behind - and the retry dies on
      "table already exists", which aborts startup with no way past it short of dropping the
      table by hand. `recordings` itself survives either way, because the copy's implicit
      transaction covers the DROP; it is the upgrade's ability to restart itself that the
      BEGIN buys. Measured on this machine both ways round (dev/changelog/937). The runner's
      own rollback-on-failure only reaches this step because the step opens the transaction;
      PRAGMA user_version is transactional too, so the stamp commits with the rebuild rather
      than after it.
    * **sqlite_sequence needs no seeding.** Copying the rows with their explicit ids leaves
      the sequence at the highest id present, so every id up to it is retired - gaps left by
      recordings deleted from the middle included. What it cannot cover is an id deleted from
      the TOP before this ran: migration 53 unlinked the rows that named it and the cascaded
      ones went with the recording, so nothing in the database remembers it existed and it
      can be issued once more. That residual is what detach_recording_references() covers,
      which is why both halves stay. Seeding by hand would only help if the rows were being
      discarded rather than copied.
    * **No indexes, triggers or views to recreate.** `recordings` carries none, which is why
      the usual step 7 of the rebuild procedure is absent rather than forgotten.

    The rename leaves the stored DDL reading `CREATE TABLE "recordings"` where a fresh
    create_all() build reads it unquoted - SQLite's own spelling of the identifier it just
    rewrote, identical in every other character. Unavoidable, since the rename is what the
    rebuild is made of.

    Foreign keys are off in this app, so the referencing tables (recording_events,
    recording_segments, channel_tests.pre_check_recording_id, alerts.recording_id) are
    untouched by the drop and keep pointing at `recordings` across the rename - their FK
    clauses name the table, and nothing references the temporary name.

    Idempotent: a table already carrying AUTOINCREMENT is left alone.
    """
    row = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='recordings'").fetchone()
    if row is None:
        return  # nothing to rebuild; create_all() will have built it in the current shape
    old_sql = row[0]
    if 'AUTOINCREMENT' in old_sql:
        return

    tmp_name = 'recordings_autoinc_new'
    new_sql = _recordings_autoincrement_ddl(old_sql, tmp_name)
    columns = ', '.join(f'"{r[1]}"' for r in cur.execute('PRAGMA table_info(recordings)'))

    if not conn.in_transaction:
        cur.execute('BEGIN')
    cur.execute(new_sql)
    cur.execute(f'INSERT INTO {tmp_name} ({columns}) SELECT {columns} FROM recordings')
    copied = cur.rowcount
    cur.execute('DROP TABLE recordings')
    cur.execute(f'ALTER TABLE {tmp_name} RENAME TO recordings')
    log.info('Rebuilt the recordings table with AUTOINCREMENT (%d row(s) carried over); '
             'a deleted recording id is no longer re-issued', copied)


def _m056_segment_content_duration(conn, cur):
    """recording_segments: content_duration_seconds - how many seconds of content the
    segment's finished file holds, ffprobed once at concat time (app/concatenator.py).
    Compared against the segment's own wall clock it shows a feed delivering faster or
    slower than real time (dev/changelog/942).

    No backfill, deliberately. The segment .ts files are deleted after a successful
    conversion, so the only remaining source for an existing row is ffmpeg's last
    `time=` progress line inside the stored capture_stderr diagnostic - a regex over a
    tail that read_stderr_tail may have truncated. Filling the column from that would put
    two measurement methods of different precision behind one name, and a NULL that means
    "not measured" is worth more than a number nobody can reproduce."""
    seg_cols = [r[1] for r in cur.execute('PRAGMA table_info(recording_segments)').fetchall()]
    if 'content_duration_seconds' not in seg_cols:
        cur.execute('ALTER TABLE recording_segments ADD COLUMN content_duration_seconds FLOAT')
    conn.commit()


def _m057_analysis_completion_stamp(conn, cur):
    """recordings: analysis_completed_at - the recorded fact that the post-capture analysis
    phase finished, so a service restart resumes at the conversion instead of re-probing the
    whole file and blending a duplicate capture-quality observation into the channel's health
    score (dev/changelog/951).

    The backfill keys on capture_quality_breakdown, which is the column the phase's final
    commit writes. That artifact is deliberately NOT what the running app gates on - it is
    absent whenever recording.gather_health_data is off, which is why the stamp exists - but
    for rows written before this column existed it is the only evidence there is, and it is
    exact in the one direction that matters: a row carrying it reached the end of the phase.
    Leaving those rows NULL is not the neutral choice - the two recordings still parked in
    ANALYZING when this shipped would have re-analyzed and re-blended on the very next
    restart, re-corrupting the scores this same migration's repair obligation exists to fix.

    The stamp's value is the recording's last POSTCAPTURE_ANALYSIS_STARTED event, the closest
    recorded moment to the completion nobody wrote down; it is never later than the true one.
    """
    rec_cols = [r[1] for r in cur.execute('PRAGMA table_info(recordings)').fetchall()]
    added = False
    if 'analysis_completed_at' not in rec_cols:
        _register_backfill(conn, cur, _BF_ANALYSIS_COMPLETED)
        cur.execute('ALTER TABLE recordings ADD COLUMN analysis_completed_at DATETIME')
        added = True
    if _backfill_needed(cur, _BF_ANALYSIS_COMPLETED, added):
        cur.execute(
            'UPDATE recordings SET analysis_completed_at = COALESCE('
            '  (SELECT MAX(e.timestamp) FROM recording_events e '
            '    WHERE e.recording_id = recordings.id '
            "      AND e.event_type = 'POSTCAPTURE_ANALYSIS_STARTED'), "
            '  completed_at, ?) '
            'WHERE capture_quality_breakdown IS NOT NULL '
            '  AND analysis_completed_at IS NULL', (_utc_stamp(),))
        _finish_backfill(conn, cur, _BF_ANALYSIS_COMPLETED)

    # The scores already double-counted cannot be repaired from here: undoing a blend is a
    # full replay of the channel's observation ledger, which is ORM code (health_score is a
    # lossy exponential average - nothing can be subtracted back out of it). So this step
    # records the obligation and create_app() discharges it once the ORM is up.
    _register_backfill(conn, cur, _BF_DUPLICATE_CAPTURE_CORRECTIONS)
    conn.commit()


def _m058_postprocess_waiting_stamp(conn, cur):
    """recordings: postprocess_waiting_since - when this recording's post-processing parked
    itself to let another recording have the local CPU and disk, NULL while it is working.
    It is what lets tools/check_busy.py tell a parked recording from a working one, which it
    cannot do from status alone (dev/changelog/952).

    No backfill: NULL already means "not waiting", which is true of every row that exists
    when this runs. A row genuinely parked at upgrade time is mid-flight in a process this
    migration is about to replace, and the startup sweep clears the column for those anyway.
    """
    rec_cols = [r[1] for r in cur.execute('PRAGMA table_info(recordings)').fetchall()]
    if 'postprocess_waiting_since' not in rec_cols:
        cur.execute('ALTER TABLE recordings ADD COLUMN postprocess_waiting_since DATETIME')
    conn.commit()


def _m059_postprocess_waiting_blocker(conn, cur):
    """recordings: postprocess_waiting_on_name / postprocess_waiting_on_state - who the park
    recorded by migration 58 is waiting on, and what that recording was doing.

    The wait was already recorded; only the pages could not say what it was for, so a parked
    recording kept describing the phase it had stopped in (dev/changelog/954).

    No backfill, for the same reason 58 has none: NULL beside a NULL
    postprocess_waiting_since means "not waiting", which is true of every row at upgrade
    time, and the startup sweep clears all three together for anything mid-flight.
    """
    rec_cols = [r[1] for r in cur.execute('PRAGMA table_info(recordings)').fetchall()]
    if 'postprocess_waiting_on_name' not in rec_cols:
        cur.execute('ALTER TABLE recordings ADD COLUMN postprocess_waiting_on_name VARCHAR(500)')
    if 'postprocess_waiting_on_state' not in rec_cols:
        cur.execute('ALTER TABLE recordings ADD COLUMN postprocess_waiting_on_state VARCHAR(200)')
    conn.commit()


def _m060_conversion_parts_checkpoint(conn, cur):
    """recordings: conversion_parts_done / conversion_source_covered_seconds /
    conversion_source_complete / conversion_parts_signature - the recorded checkpoint that
    lets a killed re-encode resume where it stopped instead of starting over
    (dev/changelog/955).

    No backfill, and the defaults are what make that safe: 0 parts covering NULL seconds is
    "nothing is checkpointed", which is true of every row that exists when this runs. A row
    genuinely mid-conversion at upgrade time was converting under the old whole-file command,
    so its partial has no moov atom and is not a checkpoint under any reading - it re-encodes
    from the start exactly as it would have before, which is the behavior this migration is
    replacing rather than a regression from it.
    """
    rec_cols = [r[1] for r in cur.execute('PRAGMA table_info(recordings)').fetchall()]
    if 'conversion_parts_done' not in rec_cols:
        cur.execute('ALTER TABLE recordings ADD COLUMN conversion_parts_done INTEGER DEFAULT 0')
    if 'conversion_source_covered_seconds' not in rec_cols:
        cur.execute('ALTER TABLE recordings ADD COLUMN conversion_source_covered_seconds FLOAT')
    if 'conversion_source_complete' not in rec_cols:
        cur.execute('ALTER TABLE recordings ADD COLUMN conversion_source_complete BOOLEAN DEFAULT 0')
    if 'conversion_parts_signature' not in rec_cols:
        cur.execute('ALTER TABLE recordings ADD COLUMN conversion_parts_signature VARCHAR(64)')
    conn.commit()


def _m061_segment_exclusion(conn, cur):
    """recording_segments: excluded_reason, and its two Recording-level rollups
    discarded_segment_count / discarded_seconds - a segment that was captured but kept out
    of the final file, because the provider served its "channel offline" placeholder clip
    instead of the channel (dev/changelog/957).

    No backfill, and this is the case where that is a substantive choice rather than a
    convenience. The rows a backfill would want are identifiable - a segment whose
    content_duration_seconds dwarfs its wall clock and which exited 0 - but those recordings
    have already been joined, converted and had their segment files deleted, so marking a
    row excluded now would change what covered_capture_seconds and the capture gap report
    about a file that demonstrably does contain those minutes. A NULL here means "not
    evaluated", which is the truth for every row that predates the detector, and the
    finished artifact keeps being described honestly.
    """
    seg_cols = [r[1] for r in cur.execute('PRAGMA table_info(recording_segments)').fetchall()]
    if 'excluded_reason' not in seg_cols:
        cur.execute('ALTER TABLE recording_segments ADD COLUMN excluded_reason VARCHAR(64)')
    rec_cols = [r[1] for r in cur.execute('PRAGMA table_info(recordings)').fetchall()]
    if 'discarded_segment_count' not in rec_cols:
        cur.execute('ALTER TABLE recordings ADD COLUMN discarded_segment_count INTEGER')
    if 'discarded_seconds' not in rec_cols:
        cur.execute('ALTER TABLE recordings ADD COLUMN discarded_seconds FLOAT')
    conn.commit()


def _m062_fast_delivery_rollup(conn, cur):
    """recordings: fast_delivery_segment_count / fast_delivery_seconds - how many joined
    segments delivered more content than the seconds they ran for, and how much of the
    finished file came from them (dev/changelog/966).

    No backfill, for the same reason migration 61 gave and one more. The rows are derivable
    (content_duration_seconds minus the segment's wall span), but the number is only
    meaningful against the threshold in force when the join ran, and every recording old
    enough to need a backfill was already presented to the user as clean. NULL means "not
    evaluated", which is true of every row captured before the check existed; 0 means the
    join looked and found none, which is a different answer and is only ever written by
    code that actually looked.
    """
    rec_cols = [r[1] for r in cur.execute('PRAGMA table_info(recordings)').fetchall()]
    if 'fast_delivery_segment_count' not in rec_cols:
        cur.execute('ALTER TABLE recordings ADD COLUMN fast_delivery_segment_count INTEGER')
    if 'fast_delivery_seconds' not in rec_cols:
        cur.execute('ALTER TABLE recordings ADD COLUMN fast_delivery_seconds FLOAT')
    conn.commit()


def _m063_channel_pace_realtime(conn, cur):
    """channels: pace_realtime, the per-channel answer to "read this stream at real-time
    speed" (dev/changelog/997).

    No backfill: NULL means "follow ffmpeg.pace_realtime in Settings", which is exactly how
    every existing channel behaved before the column existed.
    """
    ch_cols = [r[1] for r in cur.execute('PRAGMA table_info(channels)').fetchall()]
    if 'pace_realtime' not in ch_cols:
        cur.execute('ALTER TABLE channels ADD COLUMN pace_realtime BOOLEAN')
    conn.commit()


def _m064_account_stat_ledger(conn, cur):
    """account_stat_days + account_stat_state: the per-account per-day ledger behind the
    Accounts pages' usage numbers (app/account_stats.py, dev/changelog/1028).

    No backfill here and no obligation to register: the ledger is filled by
    `account_stats.refresh_ledger()` from its own watermarks, which start at 0, so the first
    page load (or the hourly fold) after the upgrade reads the whole history. The watermark
    row is the record of progress; an empty table with no state row simply means "nothing
    folded yet", which is true.

    CREATE TABLE/INDEX IF NOT EXISTS throughout, so this step is re-runnable from the top."""
    cur.execute('''
        CREATE TABLE IF NOT EXISTS account_stat_days (
            id              INTEGER PRIMARY KEY,
            account_id      INTEGER NOT NULL REFERENCES accounts(id),
            day             VARCHAR(10) NOT NULL,
            capture_seconds FLOAT NOT NULL,
            segments        INTEGER NOT NULL,
            recordings      INTEGER NOT NULL,
            stalls          INTEGER NOT NULL,
            checks_passed   INTEGER NOT NULL,
            checks_failed   INTEGER NOT NULL,
            failovers_away  INTEGER NOT NULL,
            CONSTRAINT uq_account_stat_day UNIQUE (account_id, day)
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS account_stat_state (
            id                 INTEGER PRIMARY KEY,
            segment_watermark  INTEGER NOT NULL,
            test_watermark     INTEGER NOT NULL,
            event_watermark    INTEGER NOT NULL,
            tz_name            VARCHAR(64),
            refreshed_at       DATETIME,
            rebuild_started_at DATETIME,
            rebuilt_at         DATETIME
        )
    ''')
    conn.commit()


def _m065_channel_account_epg_index(conn, cur):
    """channels: ix_channels_account_epg, so the Accounts list's per-account EPG coverage
    count is answered from an index instead of every channel row. Measured on a copy of the
    production database (139,101 channels): 162 ms -> 23 ms for the statement; the index
    took 0.19 s to build there (dev/changelog/1029).

    CREATE INDEX IF NOT EXISTS is idempotent, so this step is re-runnable from the top."""
    cur.execute('CREATE INDEX IF NOT EXISTS ix_channels_account_epg '
                'ON channels (account_id, epg_channel_id)')
    conn.commit()


def _m066_recording_cancel_reason(conn, cur):
    """recordings: cancel_reason, the enumerated answer to why a row is ABORTED.

    No backfill. NULL means a row cancelled before the vocabulary existed, and the detail
    page's CANCELLED strip reads those off the disk rather than guessing - the cancel
    reason is not recoverable from a row's own columns, and inferring one from an event's
    prose would invent history (dev/changelog/1054).

    Column-presence guarded, so this step is re-runnable from the top."""
    existing = [r[1] for r in cur.execute('PRAGMA table_info(recordings)').fetchall()]
    if 'cancel_reason' not in existing:
        cur.execute('ALTER TABLE recordings ADD COLUMN cancel_reason VARCHAR(64)')
    conn.commit()


def _m067_recording_metadata_snapshot(conn, cur):
    """recordings: metadata_description/_category/_rating + metadata_locked.

    No backfill, and that is the honest answer rather than a shortcut. The values come
    off epg_entries, which epg.epg_keep_days deletes within a day of a program airing, so
    a backfill would reach only the handful of recordings whose listing happens to survive
    and would look like it had done the whole job. Recordings still SCHEDULED when this
    lands need no backfill at all - the record-start refresh in recorder.start_recording()
    fills them in on the way past (dev/changelog/1055).

    Column-presence guarded, so this step is re-runnable from the top."""
    existing = [r[1] for r in cur.execute('PRAGMA table_info(recordings)').fetchall()]
    if 'metadata_description' not in existing:
        cur.execute('ALTER TABLE recordings ADD COLUMN metadata_description TEXT')
    if 'metadata_category' not in existing:
        cur.execute('ALTER TABLE recordings ADD COLUMN metadata_category VARCHAR(255)')
    if 'metadata_rating' not in existing:
        cur.execute('ALTER TABLE recordings ADD COLUMN metadata_rating VARCHAR(64)')
    if 'metadata_locked' not in existing:
        cur.execute('ALTER TABLE recordings ADD COLUMN metadata_locked '
                    'BOOLEAN NOT NULL DEFAULT 0')
    conn.commit()


def _m068_profile_metadata_sidecar(conn, cur):
    """recording_profiles: metadata_sidecar_enabled, the tri-state per-profile override of
    recording.metadata_sidecar.enabled.

    Nullable with no default on purpose: NULL means "inherit the global setting", which is
    what every existing profile should do when this lands. A DEFAULT 0 here would read as
    "this profile has turned the feature off", so every profile would keep the feature off
    for its recordings after the user switched it on globally (dev/changelog/1057).

    Column-presence guarded, so this step is re-runnable from the top."""
    existing = [r[1] for r in cur.execute('PRAGMA table_info(recording_profiles)').fetchall()]
    if 'metadata_sidecar_enabled' not in existing:
        cur.execute('ALTER TABLE recording_profiles ADD COLUMN metadata_sidecar_enabled BOOLEAN')
    conn.commit()


def _m069_recording_metadata_edit(conn, cur):
    """recordings: metadata_title + metadata_sidecar_enabled, the two columns the
    preview-and-correct surface writes (dev/changelog/1058).

    Both nullable with no default, and for the same reason as migration 68's column above:
    NULL is the meaningful resting state. metadata_title NULL means "derive the written
    title from program_title and program_sub_title", which is what every recording made
    before this did and should keep doing; metadata_sidecar_enabled NULL means "inherit the
    profile, and through it the global setting". A DEFAULT on either would turn an absence
    into an answer the user never gave, on every existing row at once.

    No backfill, so no entry in the obligation ledger: there is nothing to compute here,
    only two columns to add.

    Column-presence guarded, so this step is re-runnable from the top."""
    existing = [r[1] for r in cur.execute('PRAGMA table_info(recordings)').fetchall()]
    if 'metadata_title' not in existing:
        cur.execute('ALTER TABLE recordings ADD COLUMN metadata_title VARCHAR(512)')
    if 'metadata_sidecar_enabled' not in existing:
        cur.execute('ALTER TABLE recordings ADD COLUMN metadata_sidecar_enabled BOOLEAN')
    conn.commit()


def _m070_profile_poster(conn, cur):
    """recording_profiles: poster_file + poster_width + poster_height, the poster image a
    profile can pin for every recording made under it (dev/changelog/1059).

    All three nullable with no default: NULL on poster_file is the resting state, "no
    pinned poster, use the captured frame", which is what every existing profile does. The
    size columns exist because the profiles page displays them and would otherwise have to
    open each poster file per row.

    No backfill, so no entry in the obligation ledger. Column-presence guarded, so this
    step is re-runnable from the top."""
    existing = [r[1] for r in cur.execute('PRAGMA table_info(recording_profiles)').fetchall()]
    if 'poster_file' not in existing:
        cur.execute('ALTER TABLE recording_profiles ADD COLUMN poster_file VARCHAR(255)')
    if 'poster_width' not in existing:
        cur.execute('ALTER TABLE recording_profiles ADD COLUMN poster_width INTEGER')
    if 'poster_height' not in existing:
        cur.execute('ALTER TABLE recording_profiles ADD COLUMN poster_height INTEGER')
    conn.commit()


def _m071_one_check_per_group(conn, cur):
    """Every stored group carries exactly one health check, and the `health_check_only`
    format strategy is gone (dev/changelog/1077, DESIGN-channel-groups-model.md 4.4, 6).

    Three things, then the index that makes the first permanent:

    1. `format_strategy='health_check_only'` becomes `highest_score`, the new default.
       Whether a group records is a fact about its members (one has Recording on), never
       a stored value the switches could contradict.
    2. A non-system group with no job gets one - QUEUED, named `<group> - health check`,
       no schedule - exactly what channel_groups.build_group_with_members() mints now.
    3. A group with more than one job keeps ONE: the one with a live schedule, else the
       most recently completed, else the newest. The losers' channel_tests rows are
       re-pointed at the kept job so no history is lost, then the loser rows go, along
       with any APScheduler entry a loser still held.
    4. UNIQUE INDEX on on_demand_test_jobs.group_id.

    Deliberately blunt: there are two databases in the world and both belong to the one
    user. Every part is re-runnable from the top - a group already at one job is not
    touched - and a WARNING names each group this changed, because a check that was
    merged away is a thing its owner must not have to infer."""
    rewritten = cur.execute(
        "UPDATE channel_groups SET format_strategy='highest_score' "
        "WHERE format_strategy='health_check_only'").rowcount
    if rewritten:
        log.warning('Migration 71: %d group(s) moved off the retired health_check_only '
                    'strategy to highest_score; whether a group records is now read from '
                    'its members alone', rewritten)

    groups = cur.execute(
        'SELECT id, name FROM channel_groups WHERE is_system=0 ORDER BY id').fetchall()
    jobs_by_group = {}
    for row in cur.execute(
            'SELECT id, group_id, status, recurring, recur_paused, completed_at, '
            'scheduler_job_id FROM on_demand_test_jobs WHERE is_system=0 '
            'AND group_id IS NOT NULL ORDER BY id').fetchall():
        jobs_by_group.setdefault(row[1], []).append(row)

    has_jobstore = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='apscheduler_jobs'"
    ).fetchone() is not None

    for gid, name in groups:
        jobs = jobs_by_group.get(gid, [])
        if not jobs:
            cur.execute(
                'INSERT INTO on_demand_test_jobs (name, is_system, status, created_at, '
                'recurring, recur_paused, recur_use_window, group_id) '
                "VALUES (?, 0, 'QUEUED', CURRENT_TIMESTAMP, 0, 0, 0, ?)",
                (f'{name} - health check', gid))
            log.warning('Migration 71: group "%s" (id %d) had no health check; one was '
                        'created with no schedule', name, gid)
            continue
        if len(jobs) == 1:
            continue

        def _rank(j):
            _id, _gid, status, recurring, paused, completed_at, _sid = j
            live = status == 'SCHEDULED' and not paused
            return (1 if live else 0, completed_at or '', _id)
        kept = max(jobs, key=_rank)
        losers = [j for j in jobs if j[0] != kept[0]]
        for loser in losers:
            cur.execute('UPDATE channel_tests SET job_id=? WHERE job_id=?',
                        (kept[0], loser[0]))
            if has_jobstore and loser[6]:
                cur.execute('DELETE FROM apscheduler_jobs WHERE id=?', (loser[6],))
            cur.execute('DELETE FROM on_demand_test_jobs WHERE id=?', (loser[0],))
        log.warning('Migration 71: group "%s" (id %d) carried %d health checks; kept job '
                    '%d and merged the test history of job(s) %s into it',
                    name, gid, len(jobs), kept[0],
                    ', '.join(str(j[0]) for j in losers))

    cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_on_demand_test_jobs_group '
                'ON on_demand_test_jobs (group_id)')
    conn.commit()


SCHEMA_MIGRATIONS = [
    (1, 'baseline: pre-versioning additive migrations + backfills', _m001_baseline),
    (2, 'recordings: program_title/program_sub_title snapshot columns + backfill', _m002_program_title),
    (3, 'channels/recordings: group_id columns for manual channel grouping', _m003_channel_group_columns),
    (4, 'unify guide health checks: system on-demand job row + guide-test backfill', _m004_unify_guide_health_checks),
    (5, 'channel_groups: group-as-a-whole health_score columns', _m005_channel_group_health),
    (6, 'channel groups: format lock + member disable state', _m006_group_format_and_disable),
    (7, 'recording_profiles: per-profile retention_days override', _m007_profile_retention),
    (8, 'recording_segments: capture-time probe columns (resolution/fps/audio)', _m008_segment_probe_columns),
    (9, 'channel_groups: kind + is_system (groups unification 1/4, additive)', _m009_group_kind_columns),
    (10, 'channels: membership backfill + drop legacy group columns (groups unification 2/4)',
     _m010_membership_backfill_and_drop),
    (11, 'on_demand_test_jobs: attach to groups + drop JSON channel lists (groups unification 3/4)',
     _m011_jobs_attach_to_groups),
    (12, 'channel_tests/recording_profiles: pre-recording health check columns',
     _m012_prerecord_checks),
    (13, 'channels: first_seen_at/last_seen_at lifecycle tracking columns + backfill',
     _m013_channel_lifecycle),
    (14, 'accounts: url_normalization boolean -> mode string (disabled/mpegts/mpegts_live/hls)',
     _m014_url_normalization_mode),
    (15, 'channel_tests: stream quality profile columns (codec/pix_fmt/interlaced/vfr/gaps)',
     _m015_channel_test_quality_profile),
    (16, 'recordings: supervised-conversion monitor state (attempts + progress snapshot)',
     _m016_conversion_progress),
    (17, 'epg_entries: composite index on (channel_id, stop_time) for /channels/epg',
     _m017_epg_composite_index),
    (18, 'accounts: sync_interval_hours/sync_enabled/max_connections (folded from scheduler.py _migrate_db)',
     _m018_account_sync_columns),
    (19, 'accounts: constructed_stream_url_count cache column', _m019_account_constructed_url_count),
    (20, 'recordings/recording_segments: seek-scan timeline stats + stream format profile',
     _m020_recording_diagnostics_and_format),
    (21, 'FTS5 trigram search indexes: ch_fts, chan_prog + chan_prog_fts, and the first build',
     _m021_search_indexes),
    (22, 'search_index_state: source_watermark staleness column + a rebuild that records it',
     _m022_search_index_watermark),
    (23, 'chan_prog/chan_prog_fts: program description column + chunked rebuild',
     _m023_chan_prog_description),
    (24, 'channels: url_normalizable column + the channel-search facet indexes',
     _m024_channel_search_support),
    (25, 'epg_entries: composite (start_time, stop_time) for the airing grain first page',
     _m025_epg_start_stop_index),
    (26, 'channels: consecutive_test_failures streak column + backfill', _m026_channel_failure_streak),
    (27, 'account_sync_logs: channels_added/channels_removed breakdown columns', _m027_sync_log_added_removed),
    (28, 'recordings/recording_segments: near-empty segment detection + capture-quality breakdown', _m028_near_empty_and_capture_quality),
    (29, 'on_demand_test_jobs: maintenance-window dispatch columns (recur_use_window/last_full_run_at/window_skip_until)',
     _m029_check_window_columns),
    (30, 'channel_groups: cloned_from_group_id/cloned_from_name clone-provenance note',
     _m030_group_clone_provenance),
    (31, 'accounts: provider_* columns capturing the Xtream auth response (exp_date/is_trial/'
     'max_connections/active_cons/status/allowed_output_formats/stream_origin)',
     _m031_account_provider_info),
    (32, 'recording_segments: channel_id (which member channel captured each segment) + backfill',
     _m032_segment_channel_id),
    (33, 'accounts: xtream_debug_override, a per-account dump/replay debug toggle',
     _m033_account_xtream_debug_override),
    (34, 'channel_tests: multi-track detection (video_track_count/audio_track_count/extra_tracks)',
     _m034_channel_test_multi_track),
    (35, 'recordings: dead-stream fast-fail retry budget (dead_stream_retry_count/next_retry_at)',
     _m035_dead_stream_retry),
    (36, 'epg_entries: duration_minutes VIRTUAL generated column + index for the program-length '
     'filter', _m036_epg_duration_column),
    (37, 'channels: logo_cache_path/logo_cache_source_url for local logo caching',
     _m037_channel_logo_cache),
    (38, 'channels: search_text_updated_at + index, the channels search index watermark',
     _m038_channel_search_text_watermark),
    (39, 'drop six single-column indexes that a wider index or UNIQUE constraint already covers',
     _m039_drop_prefix_redundant_indexes),
    (40, 'channels: expression index on (lower(name), id) for the default channel-grain sort',
     _m040_channel_lower_name_index),
    (41, 'channel groups: the unified model - refuses an existing database, no upgrade path',
     _m041_channel_group_model_requires_fresh_db),
    (42, 'channel_groups: muted_warnings, the per-group hidden-banner set',
     _m042_group_muted_warnings),
    (43, 'channels: hidden/hidden_override/hidden_reason/hidden_deferred + the hidden index',
     _m043_channel_hidden_state),
    (44, 'channel_hide_rules: blanket GLOB rules over category and channel name',
     _m044_channel_hide_rules),
    (45, 'channels: drop ix_channels_hidden and ix_channels_test_enabled, unused by any query',
     _m045_drop_channels_hidden_index),
    (46, 'accounts: hidden_channel_count cache column + backfill',
     _m046_account_hidden_channel_count),
    (47, 'drop the channel-scoped CHANNEL_GROUP_FORMAT_* events + GROUP_FORMAT_MISMATCH '
     'alerts; the state is per-membership now', _m047_drop_channel_scoped_format_events),
    (48, 'channels: ix_channels_standing, the standing breakdown\'s covering index',
     _m048_channel_standing_index),
    (49, 'recording_profiles: stall_move_count/stall_move_window_minutes, the per-profile '
     'stall-rate demotion trigger', _m049_profile_stall_move_trigger),
    (50, 'channel_health_exclusions: observations a user has taken out of a channel\'s '
     'health score', _m050_channel_health_exclusions),
    (51, 'account_sync_logs: skipped_malformed_urls/skipped_duplicate_stream_ids + a backfill '
     'from the alert titles that carried them', _m051_sync_log_skip_counts),
    (52, 'dismiss open alerts of the twelve types nothing raises any more, so the counter '
     'starts honest', _m052_dismiss_retired_alert_type_rows),
    (53, 'alerts + channel_tests: unlink the rows still naming a recording they cannot be '
     'about, whose id SQLite has since re-issued to another recording',
     _m053_unlink_stale_recording_references),
    (54, 'remove the one-time TV Guide retarget announcement: its alert row, and the '
     'user_prefs flag that gated a startup hook that no longer exists',
     _m054_drop_guide_check_retarget_announcement),
    (55, 'recordings: rebuild with AUTOINCREMENT so a deleted recording id is never '
     're-issued to the next recording created',
     _m055_recordings_autoincrement),
    (56, 'recording_segments: content_duration_seconds, the segment\'s own content length '
     'measured at concat time', _m056_segment_content_duration),
    (57, 'recordings: analysis_completed_at, the recorded fact that the post-capture '
     'analysis phase finished + repair obligation for the health scores a repeated '
     'analysis double-counted', _m057_analysis_completion_stamp),
    (58, 'recordings: postprocess_waiting_since, the recorded fact that post-processing is '
     'parked waiting on another recording', _m058_postprocess_waiting_stamp),
    (59, 'recordings: postprocess_waiting_on_name/_state, who that park is waiting on and '
     'what it is doing', _m059_postprocess_waiting_blocker),
    (60, 'recordings: conversion_parts_done/_source_covered_seconds/_source_complete/'
     '_parts_signature, the recorded checkpoint a killed re-encode resumes from',
     _m060_conversion_parts_checkpoint),
    (61, 'recording_segments: excluded_reason + the recordings rollups '
     'discarded_segment_count/discarded_seconds, for a captured segment kept out of the '
     'final file because the provider served its placeholder clip',
     _m061_segment_exclusion),
    (62, 'recordings: fast_delivery_segment_count/fast_delivery_seconds, the rollup of '
     'joined segments whose video arrived faster than the clock',
     _m062_fast_delivery_rollup),
    (63, 'channels: pace_realtime, the per-channel override of ffmpeg.pace_realtime',
     _m063_channel_pace_realtime),
    (64, 'account_stat_days/account_stat_state: the per-account per-day usage ledger',
     _m064_account_stat_ledger),
    (65, 'channels: ix_channels_account_epg, the Accounts list\'s EPG coverage count',
     _m065_channel_account_epg_index),
    (66, 'recordings: cancel_reason, why a row is ABORTED - what the cancel left on disk, '
     'which the CANCELLED strip claimed was nothing', _m066_recording_cancel_reason),
    (67, 'recordings: metadata_description/_category/_rating + metadata_locked - the '
     'program\'s synopsis, genre and rating, which live only on the epg_entries row that '
     'gets pruned a day after it airs', _m067_recording_metadata_snapshot),
    (68, 'recording_profiles: metadata_sidecar_enabled, the per-profile override of '
     'whether a finished recording gets the .nfo and poster that describe it to a media '
     'server', _m068_profile_metadata_sidecar),
    (69, 'recordings: metadata_title + metadata_sidecar_enabled - the written title when '
     'the user overrides it, and the per-recording half of the sidecar on/off gate',
     _m069_recording_metadata_edit),
    (70, 'recording_profiles: poster_file + poster_width + poster_height - the poster '
     'image a profile pins for every recording made under it', _m070_profile_poster),
    (71, 'one health check per group: retire the health_check_only strategy, mint a check '
     'for every group without one, merge extras, UNIQUE on on_demand_test_jobs.group_id',
     _m071_one_check_per_group),
]

CURRENT_SCHEMA_VERSION = SCHEMA_MIGRATIONS[-1][0]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def is_fresh_db() -> bool:
    """True if this is a brand-new database. Must be called BEFORE db.create_all() -
    keyed on the recordings table, which has existed since the initial schema."""
    conn = db.engine.raw_connection()
    try:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='recordings'"
        ).fetchone()
        return row is None
    finally:
        conn.close()


def run_migrations(fresh_db: bool, cfg: dict):
    """Bring the DB to CURRENT_SCHEMA_VERSION. Called from create_app() right after
    db.create_all(). Fresh DBs are stamped current (create_all already built the full
    schema); existing DBs run any pending steps in order, snapshotting first.

    `cfg` is the merged config create_app() is already holding, and it is required rather
    than re-read here: the snapshot's destination is a filesystem path, and a path resolved
    from a fresh load_config() is resolved against the REAL install no matter whose database
    is being migrated (dev/changelog/936).
    """
    conn = db.engine.raw_connection()
    try:
        cur = conn.cursor()
        db_version = cur.execute('PRAGMA user_version').fetchone()[0]

        if fresh_db:
            cur.execute(f'PRAGMA user_version = {CURRENT_SCHEMA_VERSION}')
            conn.commit()
            log.info('Fresh database created at schema version %d (ChannelBin %s)',
                     CURRENT_SCHEMA_VERSION, __version__)
            return

        if db_version > CURRENT_SCHEMA_VERSION:
            msg = (
                f'Database is at schema version {db_version}, but this build of ChannelBin '
                f'({__version__}) only knows schema version {CURRENT_SCHEMA_VERSION}. '
                'The database was created by a newer version - upgrade the code, or restore '
                'the pre-migration DB backup that matches this version (see database.backup_dir).'
            )
            log.critical(msg)
            raise SystemExit(msg)

        pending = [(v, d, fn) for v, d, fn in SCHEMA_MIGRATIONS if v > db_version]
        if not pending:
            return

        _backup_before_migration(cur, db_version, pending[0][0], cfg)

        for version, description, fn in pending:
            log.info('Applying schema migration %d: %s', version, description)
            start = time.monotonic()
            try:
                fn(conn, cur)
                duration_ms = int((time.monotonic() - start) * 1000)
                cur.execute(f'PRAGMA user_version = {version}')
                cur.execute(
                    'INSERT INTO schema_migrations '
                    '(version, description, app_version, applied_at, duration_ms) '
                    'VALUES (?, ?, ?, ?, ?) '
                    'ON CONFLICT(version) DO UPDATE SET '
                    'description=excluded.description, app_version=excluded.app_version, '
                    'applied_at=excluded.applied_at, duration_ms=excluded.duration_ms',
                    (version, description, __version__, datetime.utcnow(), duration_ms),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                log.critical('Schema migration %d failed - startup aborted. The DB snapshot '
                             'taken before this run is intact.', version)
                raise
            log.info('Schema migration %d applied in %d ms', version, duration_ms)
    finally:
        conn.close()


def _backup_before_migration(cur, db_version: int, first_pending: int, cfg: dict):
    """Snapshot the DB via VACUUM INTO before any pending migration runs. This is the
    entire rollback story (no down-migrations exist), so a failed snapshot aborts startup
    rather than silently migrating an un-backed-up DB.

    Both paths come from the caller's `cfg`, never a fresh load_config(), and the
    destination is derived from the database being migrated rather than from the app root
    (config.db_backup_dir). The two are the same directory in production and are not the
    same directory anywhere else: a re-read - or, after that was fixed, a cfg that
    overrode only database.path - resolved every snapshot to the running install's
    instance/db-backups even when the cursor belonged to some other database, and the
    pruner then evicted the operator's real snapshots to keep three copies of an empty one
    (dev/changelog/936, dev/changelog/1052).
    """
    from .config import (resolve_app_path, ensure_private_dir, db_backup_dir,
                         legacy_app_root_db_backup_dir)
    from .tz_utils import get_display_tz
    db_cfg = cfg.get('database', {})
    if not db_cfg.get('pre_migration_backup', True):
        log.warning('database.pre_migration_backup is false - migrating from schema version '
                    '%d without a snapshot', db_version)
        return
    backup_dir = db_backup_dir(cfg)
    legacy_dir = legacy_app_root_db_backup_dir(cfg)
    if legacy_dir and glob.glob(os.path.join(legacy_dir, 'dvr-pre-schema-v*.db')):
        log.warning('Pre-migration snapshots now go to %s, beside the database being '
                    'migrated. Older snapshots are still in %s - they are left intact and '
                    'are no longer pruned; move or delete them yourself.',
                    backup_dir, legacy_dir)
    ts = datetime.now(tz=get_display_tz()).strftime('%Y-%m-%d-%H-%M-%S')
    name = f'dvr-pre-schema-v{first_pending}-{ts}.db'
    dest = os.path.join(backup_dir, name)
    # VACUUM INTO needs SQLite file locking on the destination, which network mounts
    # (/dvr is CIFS on this install) don't support - "database is locked". Snapshot to a
    # local temp file next to the DB first, then move it (a plain file copy works anywhere).
    # Transiently needs ~DB-size free space on the DB's own filesystem.
    tmp_dest = os.path.join(os.path.dirname(resolve_app_path(db_cfg['path'])), f'.{name}.tmp')
    try:
        # The snapshot carries the accounts table's plaintext provider credentials - 0700.
        ensure_private_dir(backup_dir)
        start = time.monotonic()
        cur.execute('VACUUM INTO ?', (tmp_dest,))
        shutil.move(tmp_dest, dest)
        log.info('Pre-migration DB backup created: %s (%.1fs)', dest, time.monotonic() - start)
    except Exception as exc:
        msg = (
            f'Pre-migration database backup to {dest} failed ({exc}) - refusing to migrate '
            'without a snapshot. Fix the backup location (database.backup_dir) or set '
            'database.pre_migration_backup: false to skip backups.'
        )
        try:
            os.remove(tmp_dest)
        except OSError:
            pass  # best-effort cleanup of a partial local snapshot
        log.critical(msg)
        raise SystemExit(msg)
    _prune_migration_backups(backup_dir, db_cfg.get('migration_backups_keep', 3),
                             exclude_path=dest)


def _prune_migration_backups(backup_dir: str, keep: int, exclude_path: str = None):
    """Keep the newest `keep` backups by mtime, except `exclude_path` - normally the
    snapshot _backup_before_migration just took - which is always kept regardless of
    its mtime rank. mtime alone isn't a reliable "newest" signal (clock skew, a
    touched/copied file), so the backup this run is about to depend on must not be
    prunable by mtime ordering."""
    if keep <= 0:  # 0 = keep all
        return
    exclude_path = os.path.abspath(exclude_path) if exclude_path else None
    candidates = [b for b in glob.glob(os.path.join(backup_dir, 'dvr-pre-schema-v*.db'))
                 if os.path.abspath(b) != exclude_path]
    candidates.sort(key=os.path.getmtime, reverse=True)
    remaining_keep = keep - 1 if exclude_path else keep
    for old in candidates[max(remaining_keep, 0):]:
        try:
            os.remove(old)
            log.info('Pruned old pre-migration DB backup: %s', old)
        except OSError as exc:
            log.warning('Could not prune DB backup %s: %s', old, exc)


# ---------------------------------------------------------------------------
# Baseline backfills (pre-versioning era; referenced only by _m001_baseline)
# ---------------------------------------------------------------------------

def _backfill_url_normalization():
    from .database import Channel, Account
    from .accounts import normalize_url
    accounts = Account.query.all()
    count = 0
    for acc in accounts:
        for ch in Channel.query.filter_by(account_id=acc.id).all():
            normalized = normalize_url(ch.stream_url, acc)
            if normalized != ch.stream_url:
                ch.stream_url = normalized
                count += 1
    db.session.commit()
    if count:
        log.info('Backfill: normalized stream_url for %d channels', count)


def _backfill_duplicate_flags():
    """One-time pass to compute is_duplicate_stream_url for channels synced before this
    column existed."""
    from .accounts import _recompute_duplicate_stream_urls
    _recompute_duplicate_stream_urls()
    db.session.commit()
    log.info('Backfill: computed is_duplicate_stream_url for existing channels')


def _backfill_health_scores():
    """One-time pass to compute Channel.health_score from existing ChannelTest history for
    channels synced before these columns existed.

    Scoped to test-derived history only - reconstructing which historical FAILED recordings
    were genuinely channel-attributable (vs. a local disk-space/DVR-dir/conversion issue)
    from stored data alone isn't reliable, so recordings start contributing to the score
    prospectively from here forward (see app/health_score.py) rather than being backfilled.

    Channels that already carry a score are skipped, and that is load-bearing rather than an
    optimization: blend_health_score folds each test into the channel's *current* score, so
    this is the one backfill that is an increment and not a recompute. Its ledger obligation
    (dev/changelog/686) is cleared in a separate commit from its own, so a crash in that gap
    re-runs it - which without this filter would blend every historical test onto an already
    complete score a second time and silently invent a different number.
    """
    import json
    from .database import Channel, ChannelTest
    from .health_score import score_test_quality, blend_health_score
    from .config import load_config
    cfg = load_config()
    count = 0
    for channel in Channel.query.filter(Channel.health_score.is_(None)):
        tests = ChannelTest.query.filter_by(channel_id=channel.id) \
            .order_by(ChannelTest.test_started_at).all()
        if not tests:
            continue
        for test in tests:
            result = score_test_quality(test, cfg)
            if result is None:
                continue
            quality, quality_breakdown = result
            new_score, new_count, new_updated_at, blend_breakdown = blend_health_score(
                channel, quality, test.test_started_at, 1, cfg
            )
            channel.health_score = new_score
            channel.health_score_sample_count = new_count
            channel.health_score_updated_at = new_updated_at
            test.quality_score = quality
            test.lifetime_score_after = round(new_score)
            test.quality_breakdown = json.dumps(quality_breakdown)
            test.blend_breakdown = json.dumps(blend_breakdown)
        count += 1
    db.session.commit()
    if count:
        log.info('Backfill: computed health_score for %d channels from existing test history', count)


def _backfill_consecutive_test_failures():
    """One-time pass computing each channel's current trailing consecutive-FAILED streak
    (CANCELLED rows skipped entirely - neither extending nor resetting it, same exclusion
    score_test_quality already applies) from existing ChannelTest history, for channels
    tested before this column existed.

    One query for every non-CANCELLED ChannelTest row, not one per channel - measured
    73.5s for 137,897 channels on the per-channel version (dev/changelog/478), almost all
    of it spent on channels with zero test rows. Only channels that were ever tested
    reach the streak loop below."""
    from collections import defaultdict
    from .database import Channel, ChannelTest, TEST_STATUS_CANCELLED, TEST_STATUS_FAILED
    by_channel = defaultdict(list)
    for channel_id, status in (
            db.session.query(ChannelTest.channel_id, ChannelTest.status)
            .filter(ChannelTest.status != TEST_STATUS_CANCELLED)
            .order_by(ChannelTest.channel_id, ChannelTest.test_started_at.desc())
            .all()):
        by_channel[channel_id].append(status)

    count = 0
    if by_channel:
        for channel in Channel.query.filter(Channel.id.in_(by_channel.keys())).all():
            streak = 0
            for status in by_channel[channel.id]:
                if status != TEST_STATUS_FAILED:
                    break
                streak += 1
            if streak:
                channel.consecutive_test_failures = streak
                count += 1
    db.session.commit()
    if count:
        log.info('Backfill: computed consecutive_test_failures for %d channels from existing '
                 'test history', count)
