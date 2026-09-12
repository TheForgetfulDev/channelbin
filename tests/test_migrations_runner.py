"""Tier 2 - schema migration runner behavior (dev/changelog/268, chunk 5).

Pins the runner contract in app/migrations.py:

  * A fresh DB (built by create_all) is stamped straight to CURRENT_SCHEMA_VERSION with
    no migration steps run and an empty schema_migrations audit log.
  * A DB whose user_version is AHEAD of what this build knows refuses to start
    (downgrade protection) - run_migrations raises SystemExit rather than touching it.
  * The pre-migration snapshot lands in backup_dir via the temp-then-move path (VACUUM
    INTO to a local .tmp next to the DB, then move) and leaves no .tmp behind.
"""
import contextlib
import glob
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import migrations as M  # noqa: E402


def _user_version():
    conn = db.engine.raw_connection()
    try:
        return conn.cursor().execute('PRAGMA user_version').fetchone()[0]
    finally:
        conn.close()


@contextlib.contextmanager
def _harmless_last_step():
    """Swap the newest migration step for a no-op, at the same version.

    The two runner tests below exercise the RUNNER - its backup snapshot and its audit
    upsert - by rewinding the stamp one version and letting the last shipped step re-run.
    That stopped working when the newest step became `_m041`, which refuses an existing
    database on purpose (dev/changelog/741) rather than migrating it. Substituting a no-op
    keeps each test asserting the thing it is named after instead of step 41's behavior,
    which `tests/test_group_participation_model.py` covers directly.
    """
    original = M.SCHEMA_MIGRATIONS
    version, description, _fn = original[-1]
    M.SCHEMA_MIGRATIONS = original[:-1] + [(version, description, lambda conn, cur: None)]
    try:
        yield
    finally:
        M.SCHEMA_MIGRATIONS = original


def _sandboxed_cfg(t, **database):
    """The `cfg` run_migrations() requires, pointed at this TestApp's temp filesystem.

    run_migrations() takes the merged config rather than re-reading it, so that the
    snapshot destination belongs to the database being migrated (dev/changelog/936).
    Passing a test app's own paths here is what makes that true for a test; the default
    keeps snapshots off, since the temp DB is built by create_all() and has no pending
    step to back up unless a test rewinds the stamp on purpose.
    """
    cfg = {
        'path': t.db_path,
        'backup_dir': os.path.join(t._tmpdir, 'db-backups'),
        'pre_migration_backup': False,
    }
    cfg.update(database)
    return {'database': cfg}


def _set_user_version(v):
    conn = db.engine.raw_connection()
    try:
        cur = conn.cursor()
        cur.execute(f'PRAGMA user_version = {v}')
        conn.commit()
    finally:
        conn.close()


class FreshDbStampTests(unittest.TestCase):
    def setUp(self):
        # fresh_schema=True: this class is the one place that must exercise the real
        # create_all + run_migrations(fresh_db=True) path. Every other test starts from
        # the preseeded schema template, where is_fresh_db() is False by construction.
        self.t = make_test_app(fresh_schema=True)

    def tearDown(self):
        self.t.cleanup()

    def test_fresh_db_stamped_current_no_steps_run(self):
        # make_test_app already ran create_all + run_migrations(fresh_db=True).
        self.assertEqual(_user_version(), M.CURRENT_SCHEMA_VERSION)
        from app.database import SchemaMigration
        self.assertEqual(SchemaMigration.query.count(), 0,
                         'a fresh DB must not record any migration steps as having run')


class SchemaTemplateFidelityTests(unittest.TestCase):
    """The preseeded schema template must be indistinguishable from a real fresh build.

    Every other DB-backed test in the suite starts from a copy of the template
    (tests/support/app.py::_template_db_path) instead of running create_all, so if the two
    ever diverge, ~500 tests quietly start asserting against a schema production never
    builds. This is the check that makes that impossible to miss.
    """

    @staticmethod
    def _schema_and_stamp():
        rows = db.session.execute(db.text(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )).fetchall()
        return [tuple(r) for r in rows], _user_version()

    def test_preseeded_schema_matches_a_from_scratch_build(self):
        fresh = make_test_app(fresh_schema=True)
        try:
            fresh_schema, fresh_stamp = self._schema_and_stamp()
        finally:
            fresh.cleanup()

        seeded = make_test_app()
        try:
            seeded_schema, seeded_stamp = self._schema_and_stamp()
        finally:
            seeded.cleanup()

        self.assertEqual(seeded_stamp, fresh_stamp,
                         'template user_version stamp differs from a fresh create_all build')
        self.assertEqual(seeded_schema, fresh_schema,
                         'the preseeded schema template has drifted from what create_all '
                         'produces - tests/support/app.py::_template_db_path')

    def test_preseeded_app_carries_the_fresh_build_seed_rows(self):
        """create_app seeds the system health job and default tags only when it sees a
        brand-new DB. A preseeded app skips both branches, so those rows must already be
        in the template - otherwise every test starts without them."""
        from app.database import ChannelGroup, OnDemandTestJob, Tag
        seeded = make_test_app()
        try:
            self.assertIsNotNone(ChannelGroup.query.filter_by(is_system=True).first(),
                                 'template is missing the pinned system check-only group')
            self.assertTrue(OnDemandTestJob.query.count(),
                            'template is missing the pinned system health job')
            self.assertTrue(Tag.query.count(), 'template is missing the seeded default tags')
        finally:
            seeded.cleanup()


class DowngradeProtectionTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_user_version_ahead_of_code_refuses_startup(self):
        _set_user_version(M.CURRENT_SCHEMA_VERSION + 5)
        with self.assertRaises(SystemExit):
            M.run_migrations(fresh_db=False, cfg=_sandboxed_cfg(self.t))


class SchemaMigrationsRegistryTests(unittest.TestCase):
    """SCHEMA_MIGRATIONS's `pending` filter (app/migrations.py) preserves list order rather
    than sorting by version, so a mis-ordered or gapped append would run steps out of
    sequence - and could stamp user_version backwards mid-run - with no error, discovered
    only at a user's next upgrade. The per-migration `..._registered_at_its_own_version`
    tests scattered through this file each pin `CURRENT_SCHEMA_VERSION == tail == max`,
    which catches an append that isn't the new maximum but not an interior mis-ordering or
    gap (e.g. 1, 3, 2, 4 still has max=4, tail=4). This is the whole-registry version of
    that guard.
    """

    def test_versions_are_strictly_increasing_and_gap_free(self):
        versions = [v for v, _desc, _fn in M.SCHEMA_MIGRATIONS]
        self.assertEqual(
            versions, list(range(1, len(versions) + 1)),
            'SCHEMA_MIGRATIONS version numbers must be exactly 1..N with no gap or '
            'reordering - the runner processes them in list order, not sorted order')


class BackupSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_snapshot_lands_in_backup_dir_via_temp_then_move(self):
        # No load_config patch: the runner takes the caller's cfg, so handing it this
        # app's own paths is the whole of what keeps the snapshot on the temp filesystem
        # (dev/changelog/936). Before that, a re-read resolved backup_dir against the real
        # install and this test had to patch load_config to stay out of it.
        cfg = _sandboxed_cfg(self.t, pre_migration_backup=True)
        backup_dir = cfg['database']['backup_dir']
        # Rewind the stamp by one so the last shipped step is "pending", forcing the
        # runner to snapshot before (idempotently) re-running it.
        _set_user_version(M.CURRENT_SCHEMA_VERSION - 1)
        with _harmless_last_step():
            M.run_migrations(fresh_db=False, cfg=cfg)

        snaps = glob.glob(os.path.join(backup_dir, 'dvr-pre-schema-v*.db'))
        self.assertEqual(len(snaps), 1, f'expected exactly one snapshot, got {snaps}')
        # No leftover local temp file next to the DB (temp-then-move completed).
        tmps = glob.glob(os.path.join(os.path.dirname(self.t.db_path), '.dvr-pre-schema-*.tmp'))
        self.assertEqual(tmps, [], f'temp snapshot file left behind: {tmps}')
        # Runner brought the stamp back to current.
        self.assertEqual(_user_version(), M.CURRENT_SCHEMA_VERSION)

    def test_a_migrating_test_app_cannot_write_into_the_real_install(self):
        """dev/docs/BUGS.md 2026-09-12 - the snapshot must land where the migrating
        database lives, not where the running install keeps its own backups.

        _backup_before_migration used to re-read load_config(), and database.backup_dir
        defaults to an app-root-relative path that resolve_app_path() anchors to the REAL
        install. So a test that rewound the stamp VACUUMed its temp database into the
        operator's instance/db-backups, dropped a .tmp beside the real dvr.db on the way,
        and then let the pruner evict the genuine snapshots to keep three copies of an
        empty test database. This drives that exact scenario with nothing patched.
        """
        app_root = os.path.dirname(os.path.dirname(os.path.abspath(M.__file__)))
        real_backup_dir = os.path.join(app_root, 'instance', 'db-backups')
        before = set(glob.glob(os.path.join(real_backup_dir, '*')))

        _set_user_version(M.CURRENT_SCHEMA_VERSION - 1)
        with _harmless_last_step():
            M.run_migrations(fresh_db=False,
                             cfg=_sandboxed_cfg(self.t, pre_migration_backup=True))

        self.assertEqual(set(glob.glob(os.path.join(real_backup_dir, '*'))), before,
                         'a test-driven migration wrote into the real install\'s backup dir')
        strays = glob.glob(os.path.join(app_root, '.dvr-pre-schema-*.tmp'))
        self.assertEqual(strays, [], f'temp snapshot left beside the real dvr.db: {strays}')


class PruneMigrationBackupsTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-08-17 - _prune_migration_backups sorted purely by mtime, so an
    older backup with a future mtime (clock skew, a touched/copied file) outranked the
    snapshot just taken for the run about to start, and pruning could delete it before the
    first migration step ran."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.backup_dir = self._tmpdir.name

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_backup(self, version, mtime):
        path = os.path.join(self.backup_dir, f'dvr-pre-schema-v{version}-x.db')
        with open(path, 'w'):
            pass
        os.utime(path, (mtime, mtime))
        return path

    def test_excluded_path_survives_even_with_the_oldest_mtime(self):
        old_but_future_mtime = self._make_backup(1, 1000)
        just_created = self._make_backup(2, 500)  # skewed clock: looks older than it is

        M._prune_migration_backups(self.backup_dir, keep=1, exclude_path=just_created)

        self.assertTrue(os.path.exists(just_created),
                        'the just-created snapshot must never be prunable, regardless of mtime')
        self.assertFalse(os.path.exists(old_but_future_mtime),
                         'keep=1 with the new snapshot excluded should prune the only other file')

    def test_keep_count_includes_the_excluded_path(self):
        excluded = self._make_backup(1, 100)
        newest = self._make_backup(2, 400)
        middle = self._make_backup(3, 300)
        oldest = self._make_backup(4, 200)

        M._prune_migration_backups(self.backup_dir, keep=3, exclude_path=excluded)

        survivors = set(glob.glob(os.path.join(self.backup_dir, 'dvr-pre-schema-v*.db')))
        self.assertEqual(survivors, {excluded, newest, middle},
                         'keep=3 should retain the excluded path plus the newest 2 others')
        self.assertFalse(os.path.exists(oldest), 'the oldest non-excluded backup should be pruned')


class AuditInsertIdempotentReRunTests(unittest.TestCase):
    """dev/docs/BUGS.md - the audit INSERT into schema_migrations must not abort a re-run
    of an already-recorded version (a hand-restored DB mixing an older user_version with a
    newer schema_migrations table, or a manually reset stamp)."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_rerunning_an_already_recorded_version_updates_the_audit_row_instead_of_raising(self):
        from app.database import SchemaMigration
        pending_version = M.CURRENT_SCHEMA_VERSION

        # Simulate a hand-restored DB: the audit table already has a row for the version
        # that's about to be (re-)applied, but user_version says it's still pending.
        conn = db.engine.raw_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                'INSERT INTO schema_migrations '
                '(version, description, app_version, applied_at, duration_ms) '
                'VALUES (?, ?, ?, ?, ?)',
                (pending_version, 'stale description', '0.0.0-stale',
                 datetime(2000, 1, 1), 1),
            )
            conn.commit()
        finally:
            conn.close()
        _set_user_version(pending_version - 1)

        with _harmless_last_step():
            # must not raise IntegrityError -> SystemExit
            M.run_migrations(fresh_db=False, cfg=_sandboxed_cfg(self.t))

        self.assertEqual(_user_version(), M.CURRENT_SCHEMA_VERSION)
        rows = SchemaMigration.query.filter_by(version=pending_version).all()
        self.assertEqual(len(rows), 1,
                         'the audit insert must upsert, not duplicate, an existing version row')
        self.assertNotEqual(rows[0].app_version, '0.0.0-stale',
                            'the audit row must record the latest application, not the stale one')


class GroupKindSchemaTests(unittest.TestCase):
    """Groups unification 1/4 (DESIGN-groups-unification.md, additive half): the
    channel_group_members join table on a fresh schema, and _m009's guarded ADD COLUMN
    path on a pre-unification DB.

    `kind` is gone (dev/changelog/741) - the era-shape tests below still name it because
    they build the table of that vintage by hand and assert what the step did to it, which
    does not change retroactively. Only the fresh-schema case tracks the current model."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_fresh_schema_shape_and_defaults(self):
        from app.database import ChannelGroupMember
        from tests.support.seed import make_account, make_channel, make_group
        acct = make_account()
        ch = make_channel(acct)
        grp = make_group(members=())
        db.session.commit()
        self.assertEqual('health_check_only', grp.format_strategy)
        self.assertFalse(grp.is_system)
        m = ChannelGroupMember(group_id=grp.id, channel_id=ch.id, position=0)
        db.session.add(m)
        db.session.commit()
        self.assertFalse(m.recording_enabled, 'a new member records nothing until a human says so')
        self.assertTrue(m.test_enabled, 'a new member is monitored by default')
        self.assertEqual([mm.channel_id for mm in grp.memberships], [ch.id])

    def test_unique_group_channel_pair_enforced(self):
        from sqlalchemy.exc import IntegrityError
        from app.database import ChannelGroupMember
        from tests.support.seed import make_account, make_channel, make_group
        acct = make_account()
        ch = make_channel(acct)
        grp = make_group(members=())
        db.session.commit()
        db.session.add(ChannelGroupMember(group_id=grp.id, channel_id=ch.id))
        db.session.commit()
        db.session.add(ChannelGroupMember(group_id=grp.id, channel_id=ch.id))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_deleting_group_deletes_membership_rows(self):
        # Teardown releases everything the create path acquired (CLAUDE.md).
        from app.database import ChannelGroupMember
        from tests.support.seed import make_account, make_channel, make_group
        acct = make_account()
        ch = make_channel(acct)
        grp = make_group(members=())
        db.session.commit()
        db.session.add(ChannelGroupMember(group_id=grp.id, channel_id=ch.id))
        db.session.commit()
        db.session.delete(grp)
        db.session.commit()
        self.assertEqual(ChannelGroupMember.query.count(), 0)

    def test_m009_adds_columns_and_is_idempotent(self):
        # Simulate a pre-unification channel_groups table in a raw scratch DB; _m009
        # only touches channel_groups, so a minimal shape suffices.
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
            cur = conn.cursor()
            cur.execute('CREATE TABLE channel_groups ('
                        'id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, '
                        'in_guide BOOLEAN NOT NULL)')
            cur.execute("INSERT INTO channel_groups (name, in_guide) VALUES ('G', 1)")
            conn.commit()
            M._m009_group_kind_columns(conn, cur)
            conn.commit()
            cols = {r[1] for r in cur.execute('PRAGMA table_info(channel_groups)')}
            self.assertIn('kind', cols)
            self.assertIn('is_system', cols)
            kind, is_system = cur.execute(
                'SELECT kind, is_system FROM channel_groups').fetchone()
            self.assertEqual(kind, 'channel', 'existing groups backfill to kind=channel')
            self.assertEqual(is_system, 0)
            # Second run must be a no-op, not an error.
            M._m009_group_kind_columns(conn, cur)
            conn.commit()
            conn.close()

    def test_m010_backfills_memberships_and_drops_legacy_columns(self):
        # Groups unification 2/4: legacy single-FK membership copies into
        # channel_group_members (position = id order per group, disabled_reason
        # preserved), then channels.group_id/group_disabled_reason are dropped.
        # Raw scratch DB shaped like the live tables at user_version 9.
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
            cur = conn.cursor()
            cur.execute('CREATE TABLE channels ('
                        'id INTEGER PRIMARY KEY, name VARCHAR(512), '
                        'group_id INTEGER, group_disabled_reason VARCHAR(32))')
            cur.execute('CREATE TABLE channel_group_members ('
                        'id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL, '
                        'channel_id INTEGER NOT NULL, position INTEGER NOT NULL, '
                        'disabled_reason VARCHAR(32), created_at DATETIME, '
                        'CONSTRAINT uq_group_member UNIQUE (group_id, channel_id))')
            cur.executemany('INSERT INTO channels (id, name, group_id, group_disabled_reason) '
                            'VALUES (?, ?, ?, ?)', [
                                (10, 'A', 1, None),
                                (20, 'B', 1, 'auto_mismatch'),
                                (30, 'C', 2, 'manual'),
                                (40, 'D', None, None),   # ungrouped: no membership row
                            ])
            conn.commit()
            M._m010_membership_backfill_and_drop(conn, cur)
            conn.commit()

            rows = cur.execute('SELECT group_id, channel_id, position, disabled_reason '
                               'FROM channel_group_members ORDER BY group_id, position').fetchall()
            self.assertEqual(rows, [
                (1, 10, 0, None),
                (1, 20, 1, 'auto_mismatch'),
                (2, 30, 0, 'manual'),
            ])
            cols = {r[1] for r in cur.execute('PRAGMA table_info(channels)')}
            self.assertNotIn('group_id', cols)
            self.assertNotIn('group_disabled_reason', cols)
            # Non-membership channel data survives the drops.
            self.assertEqual(cur.execute('SELECT COUNT(*) FROM channels').fetchone()[0], 4)
            # Second run must be a no-op, not an error (guarded on the dropped columns).
            M._m010_membership_backfill_and_drop(conn, cur)
            conn.commit()
            self.assertEqual(cur.execute(
                'SELECT COUNT(*) FROM channel_group_members').fetchone()[0], 3)
            conn.close()

    def test_m011_attaches_jobs_to_groups_and_drops_json_columns(self):
        # Groups unification 3/4: each job's stored channel list becomes a check_only
        # group's membership (position = list index, disabled ids -> 'manual'), the
        # system job gets the pinned is_system group with NO stored membership (its
        # membership is computed at run time), then both JSON columns are dropped.
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
            cur = conn.cursor()
            cur.execute('CREATE TABLE channels (id INTEGER PRIMARY KEY, name VARCHAR(512))')
            cur.execute('CREATE TABLE channel_groups ('
                        'id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, '
                        'kind VARCHAR(32), is_system BOOLEAN, in_guide BOOLEAN NOT NULL, '
                        'guide_sort_order INTEGER, auto_disable_mismatched BOOLEAN, '
                        'health_score_sample_count INTEGER, '
                        'created_at DATETIME, updated_at DATETIME)')
            cur.execute('CREATE TABLE channel_group_members ('
                        'id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL, '
                        'channel_id INTEGER NOT NULL, position INTEGER NOT NULL, '
                        'disabled_reason VARCHAR(32), created_at DATETIME, '
                        'CONSTRAINT uq_group_member UNIQUE (group_id, channel_id))')
            cur.execute('CREATE TABLE on_demand_test_jobs ('
                        'id INTEGER PRIMARY KEY, name VARCHAR(255), is_system BOOLEAN, '
                        'channel_ids_json TEXT, disabled_channel_ids_json TEXT)')
            cur.executemany('INSERT INTO channels (id, name) VALUES (?, ?)',
                            [(10, 'A'), (20, 'B'), (30, 'C')])
            cur.executemany('INSERT INTO on_demand_test_jobs '
                            '(id, name, is_system, channel_ids_json, disabled_channel_ids_json) '
                            'VALUES (?, ?, ?, ?, ?)', [
                                # Deliberately NOT in id order - position must follow the
                                # JSON list order, not the channel id.
                                (1, 'Sports', 0, '[30, 10, 20]', '[10]'),
                                (2, 'TV Guide Channels', 1, '[]', None),
                                # 99 no longer exists: a stale id must be skipped, not
                                # inserted as a dangling membership row.
                                (3, 'Stale', 0, '[20, 99]', None),
                            ])
            conn.commit()
            M._m011_jobs_attach_to_groups(conn, cur)
            conn.commit()

            def _group_of(job_id):
                return cur.execute('SELECT group_id FROM on_demand_test_jobs WHERE id=?',
                                   (job_id,)).fetchone()[0]

            def _members(gid):
                return cur.execute('SELECT channel_id, position, disabled_reason '
                                   'FROM channel_group_members WHERE group_id=? '
                                   'ORDER BY position', (gid,)).fetchall()

            sports_gid = _group_of(1)
            self.assertEqual(_members(sports_gid),
                             [(30, 0, None), (10, 1, 'manual'), (20, 2, None)],
                             'membership order must mirror channel_ids_json order, and '
                             'disabled ids must land as disabled_reason=manual')
            name, kind, is_system = cur.execute(
                'SELECT name, kind, is_system FROM channel_groups WHERE id=?',
                (sports_gid,)).fetchone()
            self.assertEqual((name, kind, is_system), ('Sports', 'check_only', 0))

            system_gid = _group_of(2)
            sys_name, sys_is_system = cur.execute(
                'SELECT name, is_system FROM channel_groups WHERE id=?',
                (system_gid,)).fetchone()
            self.assertEqual(sys_name, 'TV Guide Channels')
            self.assertEqual(sys_is_system, 1)
            self.assertEqual(_members(system_gid), [],
                             'the system group stores NO membership rows - its channel '
                             'list is computed at run/display time')

            self.assertEqual(_members(_group_of(3)), [(20, 0, None)],
                             'a channel id with no surviving channels row is skipped')

            cols = {r[1] for r in cur.execute('PRAGMA table_info(on_demand_test_jobs)')}
            self.assertIn('group_id', cols)
            self.assertNotIn('channel_ids_json', cols)
            self.assertNotIn('disabled_channel_ids_json', cols)

            # Second run must be a no-op, not an error (guarded on the dropped columns).
            M._m011_jobs_attach_to_groups(conn, cur)
            conn.commit()
            self.assertEqual(cur.execute(
                'SELECT COUNT(*) FROM channel_groups').fetchone()[0], 3)
            conn.close()


class SystemHealthJobInsertVintageTests(unittest.TestCase):
    """Migration 4's system-row INSERT has to work against both vintages of
    on_demand_test_jobs (dev/docs/BUGS.md 2026-08-16, dev/changelog/687).

    create_all() runs before the migration runner and builds any missing table in the
    CURRENT model's shape, so a DB old enough to predate this table arrives at step 4 with
    a table that has no channel_ids_json (dropped by _m011) and a NOT NULL
    recur_use_window (added by _m029). The frozen column list crashed on the first and
    violated the second, aborting startup unrecoverably.

    The current-vintage DDL is read from a real create_all() build rather than copied here,
    so a future NOT NULL column added to the model surfaces as a failure in this test
    instead of on a user's database.

    Three of these four fail against the pre-fix step. test_m004_against_the_era_shape_is_
    unchanged is a characterization test, not a regression guard: it passes either way on
    purpose, because what it pins is that the edit did NOT change the outcome for the
    vintage the step was written for - the bound the module docstring puts on editing a
    shipped step at all.
    """

    # The shape step 4 saw when it was written: create_all()-era columns plus everything
    # _m001_baseline adds to this table, which runs before it.
    _ERA_DDL = (
        'CREATE TABLE on_demand_test_jobs ('
        ' id INTEGER PRIMARY KEY,'
        ' name VARCHAR(512) NOT NULL,'
        ' status VARCHAR(32) NOT NULL,'
        ' created_at DATETIME,'
        ' scheduled_start_time DATETIME,'
        ' completed_at DATETIME,'
        ' channel_ids_json TEXT,'
        ' disabled_channel_ids_json TEXT,'
        ' recurring BOOLEAN NOT NULL DEFAULT 0,'
        ' recur_day INTEGER,'
        ' recur_hour INTEGER,'
        ' recur_minute INTEGER,'
        ' recur_paused BOOLEAN NOT NULL DEFAULT 0,'
        ' status_before_schedule VARCHAR(32),'
        ' scheduler_job_id VARCHAR(255),'
        ' profile_id INTEGER)'
    )

    _CHANNEL_TESTS_DDL = (
        'CREATE TABLE channel_tests ('
        ' id INTEGER PRIMARY KEY, channel_id INTEGER, job_id INTEGER, status VARCHAR(32))'
    )

    @staticmethod
    def _current_ddl():
        """The live create_all() DDL for on_demand_test_jobs, straight from the models."""
        t = make_test_app()
        try:
            return db.session.execute(db.text(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='on_demand_test_jobs'"
            )).fetchone()[0]
        finally:
            t.cleanup()

    def _run_step(self, table_ddl):
        """Build a scratch DB with `table_ddl`, run step 4, return (cursor, conn, td)."""
        import sqlite3
        import tempfile
        from unittest.mock import patch

        td = tempfile.TemporaryDirectory()
        conn = sqlite3.connect(os.path.join(td.name, 'scratch.db'))
        cur = conn.cursor()
        cur.execute(table_ddl)
        cur.execute(self._CHANNEL_TESTS_DDL)
        # Two historical guide tests (job_id NULL) for the backfill half of the step.
        cur.executemany('INSERT INTO channel_tests (id, channel_id, job_id) VALUES (?, ?, ?)',
                        [(1, 10, None), (2, 20, None)])
        conn.commit()
        # The step reads the legacy channel_testing.* keys for the system job's schedule;
        # pin them so the assertions don't depend on the real config.yaml.
        cfg = {'channel_testing': {'enabled': True, 'schedule_hour': 5, 'test_days': 3}}
        with patch('app.config.load_config', return_value=cfg):
            M._m004_unify_guide_health_checks(conn, cur)
        conn.commit()
        return cur, conn, td

    def _assert_system_row(self, cur, expect_channel_ids_json):
        cols = [r[1] for r in cur.execute('PRAGMA table_info(on_demand_test_jobs)')]
        rows = cur.execute(
            'SELECT id, name, status, recurring, recur_day, recur_hour, recur_minute, '
            'recur_paused FROM on_demand_test_jobs WHERE is_system = 1').fetchall()
        self.assertEqual(len(rows), 1, 'exactly one pinned system job row')
        _id, name, status, recurring, day, hour, minute, paused = rows[0]
        self.assertEqual(name, 'TV Guide Channels')
        self.assertEqual(status, 'SCHEDULED')
        self.assertEqual((recurring, day, hour, minute, paused), (1, 3, 5, 0, 0),
                         'schedule comes from the legacy channel_testing config keys')

        self.assertEqual(
            cur.execute('SELECT COUNT(*) FROM channel_tests WHERE job_id = ?',
                        (_id,)).fetchone()[0], 2,
            'historical guide tests (job_id NULL) are adopted by the system job')

        if expect_channel_ids_json:
            self.assertIn('channel_ids_json', cols)
            self.assertEqual(
                cur.execute('SELECT channel_ids_json FROM on_demand_test_jobs '
                            'WHERE id = ?', (_id,)).fetchone()[0], '[]',
                'the era vintage still gets the empty JSON channel list it shipped with')
        else:
            self.assertNotIn('channel_ids_json', cols)

    def test_m004_against_the_current_create_all_shape(self):
        # The regression: this raised OperationalError "no such column: channel_ids_json",
        # and once that was dropped, IntegrityError on recur_use_window's NOT NULL.
        cur, conn, td = self._run_step(self._current_ddl())
        try:
            self._assert_system_row(cur, expect_channel_ids_json=False)
            self.assertEqual(
                cur.execute('SELECT recur_use_window FROM on_demand_test_jobs '
                            'WHERE is_system = 1').fetchone()[0], 0,
                'the NOT NULL column the current model added must be supplied')
        finally:
            conn.close()
            td.cleanup()

    def test_m004_against_the_era_shape_is_unchanged(self):
        # The vintage the step was written for: editing a shipped step is only allowed
        # when it cannot change the outcome for a DB that crosses it (module docstring).
        cur, conn, td = self._run_step(self._ERA_DDL)
        try:
            self._assert_system_row(cur, expect_channel_ids_json=True)
            self.assertNotIn(
                'recur_use_window',
                [r[1] for r in cur.execute('PRAGMA table_info(on_demand_test_jobs)')],
                'the era table has no such column - the step must not invent it')
        finally:
            conn.close()
            td.cleanup()

    def test_m004_is_idempotent_on_both_shapes(self):
        for label, ddl in (('current', self._current_ddl()), ('era', self._ERA_DDL)):
            with self.subTest(shape=label):
                cur, conn, td = self._run_step(ddl)
                try:
                    from unittest.mock import patch
                    cfg = {'channel_testing': {'enabled': True, 'schedule_hour': 5,
                                               'test_days': 3}}
                    with patch('app.config.load_config', return_value=cfg):
                        M._m004_unify_guide_health_checks(conn, cur)
                    conn.commit()
                    self.assertEqual(
                        cur.execute('SELECT COUNT(*) FROM on_demand_test_jobs '
                                    'WHERE is_system = 1').fetchone()[0], 1,
                        'a second run must adopt the existing system row, not insert a '
                        'duplicate one')
                finally:
                    conn.close()
                    td.cleanup()

    def test_every_not_null_column_of_the_current_model_is_supplied(self):
        """The general hazard the module docstring names: SQLAlchemy's default= is
        Python-side and never becomes a SQL DEFAULT, so a NOT NULL column added to the
        model in future breaks this INSERT unless the step supplies it."""
        cur, conn, td = self._run_step(self._current_ddl())
        try:
            unsatisfied = [
                r[1] for r in cur.execute('PRAGMA table_info(on_demand_test_jobs)')
                if r[3] and r[4] is None and not r[5]   # notnull, no dflt_value, not pk
                and cur.execute(f'SELECT COUNT(*) FROM on_demand_test_jobs '
                                f'WHERE "{r[1]}" IS NULL').fetchone()[0]
            ]
            self.assertEqual(unsatisfied, [],
                             'step 4 left a NOT NULL column of the current model unset')
        finally:
            conn.close()
            td.cleanup()


class RecordingDiagnosticsSchemaTests(unittest.TestCase):
    """Migration 20 - recordings/recording_segments diagnostics + format profile
    (dev/changelog/330). Characterization tests, not regression guards: nothing was broken
    before, the columns simply did not exist.

    The load-bearing assertion is the last one - that the migration's column list and the
    SQLAlchemy models' column list agree. A fresh DB is built by create_all() from the
    models and stamped current with no steps run, while an existing DB only ever gets the
    migration's columns, so a column added to one and not the other produces two different
    schemas that both believe they are at version 20 - and the divergence surfaces later as
    an OperationalError on a real user's DB, not here.
    """

    _RECORDING_COLS = {
        'timeline_gap_count', 'timeline_gap_seconds', 'timeline_max_gap_seconds',
        'timeline_deficit_seconds', 'timeline_damaged',
        'recorded_video_codec', 'recorded_pix_fmt', 'recorded_bit_depth',
        'recorded_chroma_subsampling', 'recorded_interlaced', 'recorded_coded_resolution',
        'recorded_is_vfr', 'recorded_bits_per_pixel_frame',
        'recorded_audio_sample_rate', 'recorded_audio_bitrate_kbps', 'recorded_audio_language',
    }
    _SEGMENT_COLS = {
        'probe_video_codec', 'probe_pix_fmt', 'probe_bit_depth', 'probe_chroma_subsampling',
        'probe_interlaced', 'probe_coded_resolution', 'probe_is_vfr',
    }

    def test_m020_adds_columns_and_is_idempotent(self):
        # Raw scratch DB shaped like the live tables at user_version 19: _m020 only reads
        # PRAGMA table_info and issues ADD COLUMN, so a minimal shape suffices.
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
            cur = conn.cursor()
            cur.execute('CREATE TABLE recordings ('
                        'id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, '
                        'recorded_fps FLOAT)')
            cur.execute('CREATE TABLE recording_segments ('
                        'id INTEGER PRIMARY KEY, recording_id INTEGER NOT NULL, '
                        'probe_fps FLOAT)')
            cur.execute("INSERT INTO recordings (name, recorded_fps) VALUES ('r', 29.97)")
            cur.execute('INSERT INTO recording_segments (recording_id, probe_fps) VALUES (1, 29.97)')
            conn.commit()

            M._m020_recording_diagnostics_and_format(conn, cur)
            conn.commit()

            rec_cols = {r[1] for r in cur.execute('PRAGMA table_info(recordings)')}
            seg_cols = {r[1] for r in cur.execute('PRAGMA table_info(recording_segments)')}
            self.assertTrue(self._RECORDING_COLS <= rec_cols,
                            f'missing on recordings: {sorted(self._RECORDING_COLS - rec_cols)}')
            self.assertTrue(self._SEGMENT_COLS <= seg_cols,
                            f'missing on recording_segments: {sorted(self._SEGMENT_COLS - seg_cols)}')

            # No backfill: every new column is NULL on the pre-existing rows, and the
            # columns that were already there are untouched.
            row = cur.execute('SELECT timeline_gap_count, timeline_damaged, '
                              'recorded_video_codec, recorded_fps FROM recordings').fetchone()
            self.assertEqual(row, (None, None, None, 29.97))
            self.assertEqual(cur.execute(
                'SELECT probe_is_vfr, probe_fps FROM recording_segments').fetchone(),
                (None, 29.97))

            # Second run must be a no-op, not a duplicate-column error.
            M._m020_recording_diagnostics_and_format(conn, cur)
            conn.commit()
            self.assertEqual(
                {r[1] for r in cur.execute('PRAGMA table_info(recordings)')}, rec_cols)
            conn.close()

    def test_m020_registered_at_its_own_version(self):
        """Pins m020 to version 20 and the derivation of CURRENT_SCHEMA_VERSION from the
        registry tail. Deliberately NOT written as "m020 is the tail" - migrations are
        append-only, so that spelling would fail on every future step for no reason."""
        registered = {v: fn for v, _d, fn in M.SCHEMA_MIGRATIONS}
        self.assertIs(registered.get(20), M._m020_recording_diagnostics_and_format)
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, M.SCHEMA_MIGRATIONS[-1][0],
                         'CURRENT_SCHEMA_VERSION derives from the registry tail')
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, max(registered),
                         'SCHEMA_MIGRATIONS must stay in ascending version order')

    def test_m020_column_set_matches_the_models(self):
        # A create_all() DB never runs this step, so the two paths must not diverge.
        from app.database import Recording, RecordingSegment
        rec_model = {c.name for c in Recording.__table__.columns}
        seg_model = {c.name for c in RecordingSegment.__table__.columns}
        self.assertTrue(self._RECORDING_COLS <= rec_model,
                        f'in migration 20 but not on the Recording model: '
                        f'{sorted(self._RECORDING_COLS - rec_model)}')
        self.assertTrue(self._SEGMENT_COLS <= seg_model,
                        f'in migration 20 but not on the RecordingSegment model: '
                        f'{sorted(self._SEGMENT_COLS - seg_model)}')


class ChannelFailureStreakMigrationTests(unittest.TestCase):
    """Migration 26 - channels.consecutive_test_failures + backfill (dev/changelog/478).
    The raw ADD COLUMN half is a scratch-DB characterization like m020 above; the
    backfill half needs a real ORM app context (Channel.query/ChannelTest.query), so it
    is a regression guard against _backfill_consecutive_test_failures directly rather
    than driving the whole migration step."""

    def test_m026_adds_column_and_is_idempotent(self):
        # Unlike m020 above, this step's backfill is ORM-based (Channel.query), so it
        # needs a real app context even though the ALTER TABLE itself runs against a
        # throwaway scratch connection - the backfill runs against the app's own (empty)
        # test DB, which is harmless (nothing to backfill) and not what this test checks.
        import sqlite3
        import tempfile
        t = make_test_app()
        try:
            with t.app.app_context():
                with tempfile.TemporaryDirectory() as td:
                    conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
                    cur = conn.cursor()
                    cur.execute('CREATE TABLE channels (id INTEGER PRIMARY KEY, health_score FLOAT)')
                    cur.execute('INSERT INTO channels (health_score) VALUES (46.7)')
                    conn.commit()

                    M._m026_channel_failure_streak(conn, cur)

                    cols = {r[1] for r in cur.execute('PRAGMA table_info(channels)')}
                    self.assertIn('consecutive_test_failures', cols)
                    self.assertEqual(
                        cur.execute('SELECT consecutive_test_failures, health_score FROM channels')
                           .fetchone(),
                        (0, 46.7), 'new column defaults to 0 and pre-existing columns are untouched')

                    # Second run must be a no-op, not a duplicate-column error.
                    M._m026_channel_failure_streak(conn, cur)
                    self.assertEqual(
                        {r[1] for r in cur.execute('PRAGMA table_info(channels)')}, cols)
                    conn.close()
        finally:
            t.cleanup()

    def test_m026_registered_at_its_own_version(self):
        registered = {v: fn for v, _d, fn in M.SCHEMA_MIGRATIONS}
        self.assertIs(registered.get(26), M._m026_channel_failure_streak)
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, M.SCHEMA_MIGRATIONS[-1][0],
                         'CURRENT_SCHEMA_VERSION derives from the registry tail')
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, max(registered),
                         'SCHEMA_MIGRATIONS must stay in ascending version order')

    def test_backfill_computes_trailing_streak_from_existing_history(self):
        """The real-data motivation: a channel with a good prior history (score 90) but
        a live trailing streak must be flagged the moment the migration runs, not only
        after a fresh sequence of future failures."""
        t = make_test_app()
        try:
            with t.app.app_context():
                from app.database import Channel
                acct = seed.make_account()
                ch = seed.make_channel(acct, name='Backfill Channel', health_score=90)
                db.session.commit()
                # Oldest -> newest: COMPLETED, then 3 straight FAILED (streak=3).
                seed.make_channel_test(ch, status='COMPLETED')
                seed.make_channel_test(ch, status='FAILED')
                seed.make_channel_test(ch, status='FAILED')
                seed.make_channel_test(ch, status='FAILED')
                db.session.commit()
                # Simulate "before this column existed" - the migration's ALTER TABLE
                # default already gives every row 0, this just makes it explicit.
                ch.consecutive_test_failures = 0
                db.session.commit()
                channel_id = ch.id

                M._backfill_consecutive_test_failures()

                db.session.expire_all()
                self.assertEqual(
                    db.session.get(Channel, channel_id).consecutive_test_failures, 3)
        finally:
            t.cleanup()

    def test_backfill_skips_cancelled_without_breaking_the_streak(self):
        t = make_test_app()
        try:
            with t.app.app_context():
                from app.database import Channel
                acct = seed.make_account()
                ch = seed.make_channel(acct, name='Backfill Channel 2', health_score=90)
                db.session.commit()
                seed.make_channel_test(ch, status='FAILED')
                seed.make_channel_test(ch, status='CANCELLED',
                                       error_detail='Interrupted by recording start')
                seed.make_channel_test(ch, status='FAILED')
                db.session.commit()
                channel_id = ch.id

                M._backfill_consecutive_test_failures()

                db.session.expire_all()
                self.assertEqual(
                    db.session.get(Channel, channel_id).consecutive_test_failures, 2,
                    'CANCELLED must be skipped, not counted as a streak-breaking pass')
        finally:
            t.cleanup()

    def test_backfill_leaves_a_currently_passing_channel_at_zero(self):
        t = make_test_app()
        try:
            with t.app.app_context():
                from app.database import Channel
                acct = seed.make_account()
                ch = seed.make_channel(acct, name='Backfill Channel 3', health_score=90)
                db.session.commit()
                seed.make_channel_test(ch, status='FAILED')
                seed.make_channel_test(ch, status='COMPLETED')
                db.session.commit()
                channel_id = ch.id

                M._backfill_consecutive_test_failures()

                db.session.expire_all()
                self.assertEqual(
                    db.session.get(Channel, channel_id).consecutive_test_failures, 0)
        finally:
            t.cleanup()


class SyncLogAddedRemovedMigrationTests(unittest.TestCase):
    """Migration 27 - account_sync_logs.channels_added/channels_removed columns
    (dev/changelog/480). Nullable, no backfill - a scratch-DB characterization like
    m020, no ORM/app-context half needed."""

    def test_m027_adds_columns_and_is_idempotent(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
            cur = conn.cursor()
            cur.execute('CREATE TABLE account_sync_logs (id INTEGER PRIMARY KEY, '
                        'channels_synced INTEGER)')
            cur.execute('INSERT INTO account_sync_logs (channels_synced) VALUES (42)')
            conn.commit()

            M._m027_sync_log_added_removed(conn, cur)

            cols = {r[1] for r in cur.execute('PRAGMA table_info(account_sync_logs)')}
            self.assertIn('channels_added', cols)
            self.assertIn('channels_removed', cols)
            row = cur.execute(
                'SELECT channels_synced, channels_added, channels_removed '
                'FROM account_sync_logs').fetchone()
            self.assertEqual(row, (42, None, None),
                              'new columns are NULL (not tracked), pre-existing row untouched')

            # Second run must be a no-op, not a duplicate-column error.
            M._m027_sync_log_added_removed(conn, cur)
            self.assertEqual(
                {r[1] for r in cur.execute('PRAGMA table_info(account_sync_logs)')}, cols)
            conn.close()

    def test_m027_registered_at_its_own_version(self):
        registered = {v: fn for v, _d, fn in M.SCHEMA_MIGRATIONS}
        self.assertIs(registered.get(27), M._m027_sync_log_added_removed)
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, M.SCHEMA_MIGRATIONS[-1][0],
                         'CURRENT_SCHEMA_VERSION derives from the registry tail')
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, max(registered),
                         'SCHEMA_MIGRATIONS must stay in ascending version order')


class ChannelTestMultiTrackMigrationTests(unittest.TestCase):
    """Migration 34 - channel_tests.video_track_count/audio_track_count/extra_tracks
    (dev/changelog/564). Nullable, no backfill - a scratch-DB characterization like m027,
    no ORM/app-context half needed."""

    def test_m034_adds_columns_and_is_idempotent(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
            cur = conn.cursor()
            cur.execute('CREATE TABLE channel_tests (id INTEGER PRIMARY KEY, '
                        'video_codec VARCHAR(32))')
            cur.execute("INSERT INTO channel_tests (video_codec) VALUES ('h264')")
            conn.commit()

            M._m034_channel_test_multi_track(conn, cur)

            cols = {r[1] for r in cur.execute('PRAGMA table_info(channel_tests)')}
            self.assertIn('video_track_count', cols)
            self.assertIn('audio_track_count', cols)
            self.assertIn('extra_tracks', cols)
            row = cur.execute(
                'SELECT video_codec, video_track_count, audio_track_count, extra_tracks '
                'FROM channel_tests').fetchone()
            self.assertEqual(row, ('h264', None, None, None),
                              'new columns are NULL (not tracked), pre-existing row untouched')

            # Second run must be a no-op, not a duplicate-column error.
            M._m034_channel_test_multi_track(conn, cur)
            self.assertEqual(
                {r[1] for r in cur.execute('PRAGMA table_info(channel_tests)')}, cols)
            conn.close()

    def test_m034_registered_at_its_own_version(self):
        registered = {v: fn for v, _d, fn in M.SCHEMA_MIGRATIONS}
        self.assertIs(registered.get(34), M._m034_channel_test_multi_track)
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, M.SCHEMA_MIGRATIONS[-1][0],
                         'CURRENT_SCHEMA_VERSION derives from the registry tail')
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, max(registered),
                         'SCHEMA_MIGRATIONS must stay in ascending version order')


class EpgDurationColumnMigrationTests(unittest.TestCase):
    """Migration 36 - epg_entries.duration_minutes, a VIRTUAL generated column + index for
    the EPG Deep Search program-length filter (dev/changelog/594). A scratch-DB
    characterization like m034, no ORM/app-context half needed - SQLite computes the column,
    there is nothing for Python to backfill."""

    def test_m036_adds_the_generated_column_and_computes_it(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
            cur = conn.cursor()
            cur.execute('CREATE TABLE epg_entries (id INTEGER PRIMARY KEY, '
                        "start_time DATETIME, stop_time DATETIME)")
            cur.execute("INSERT INTO epg_entries (start_time, stop_time) VALUES "
                        "('2026-08-11 07:55:00', '2026-08-11 09:40:00')")
            conn.commit()

            M._m036_epg_duration_column(conn, cur)

            # PRAGMA table_info deliberately omits generated columns (confirmed empirically -
            # see the migration's own docstring); table_xinfo is the variant that includes
            # them, and is what the migration's own idempotency guard has to use too.
            self.assertNotIn('duration_minutes',
                             {r[1] for r in cur.execute('PRAGMA table_info(epg_entries)')},
                             'table_info hiding generated columns is the whole reason this '
                             'migration cannot use it for its idempotency guard')
            cols = {r[1] for r in cur.execute('PRAGMA table_xinfo(epg_entries)')}
            self.assertIn('duration_minutes', cols)
            row = cur.execute('SELECT duration_minutes FROM epg_entries').fetchone()
            self.assertEqual(row, (105,), 'SQLite computes it from start/stop, not backfill')

            indexes = {r[1] for r in cur.execute('PRAGMA index_list(epg_entries)')}
            self.assertIn('ix_epg_entries_duration', indexes)

            # Second run must be a no-op, not a duplicate-column/duplicate-index error - this
            # is the exact case that caught the table_info-vs-table_xinfo bug above.
            M._m036_epg_duration_column(conn, cur)
            self.assertEqual({r[1] for r in cur.execute('PRAGMA table_xinfo(epg_entries)')},
                             cols)
            conn.close()

    def test_m036_registered_at_its_own_version(self):
        registered = {v: fn for v, _d, fn in M.SCHEMA_MIGRATIONS}
        self.assertIs(registered.get(36), M._m036_epg_duration_column)
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, M.SCHEMA_MIGRATIONS[-1][0],
                         'CURRENT_SCHEMA_VERSION derives from the registry tail')
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, max(registered),
                         'SCHEMA_MIGRATIONS must stay in ascending version order')

    def test_m036_column_matches_the_model(self):
        """The ORM declares the same generated column (app/database.py::EPGEntry), because a
        fresh install never runs migrations - it builds straight from the model
        (CLAUDE.md's 'declared here as well as in their migrations' rule)."""
        from app.database import EPGEntry
        self.assertIn('duration_minutes', EPGEntry.__table__.columns.keys())
        index_names = {ix.name for ix in EPGEntry.__table__.indexes}
        self.assertIn('ix_epg_entries_duration', index_names)


class SegmentChannelIdMigrationTests(unittest.TestCase):
    """Migration 32 - recording_segments.channel_id + backfill (recordings/#83 follow-up):
    a channel-group recording's segments say which member channel actually captured them,
    since Recording.channel_id only ever holds whichever member is CURRENT. Raw scratch-DB
    characterization like m020/m027 above - the backfill is pure SQL (no ORM), so no app
    context is needed."""

    def _make_scratch_db(self, td):
        import sqlite3
        conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
        cur = conn.cursor()
        cur.execute('CREATE TABLE recordings (id INTEGER PRIMARY KEY, channel_id INTEGER, '
                    'group_id INTEGER)')
        cur.execute('CREATE TABLE recording_segments (id INTEGER PRIMARY KEY, '
                    'recording_id INTEGER NOT NULL, segment_number INTEGER NOT NULL, '
                    'started_at DATETIME)')
        cur.execute('CREATE TABLE recording_events (id INTEGER PRIMARY KEY, '
                    'recording_id INTEGER NOT NULL, timestamp DATETIME NOT NULL, '
                    'event_type VARCHAR(64) NOT NULL, extra_data TEXT)')
        conn.commit()
        return conn, cur

    def test_m032_adds_column_and_is_idempotent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            cur.execute('INSERT INTO recordings (id, channel_id, group_id) VALUES (1, 5, NULL)')
            cur.execute('INSERT INTO recording_segments (id, recording_id, segment_number) '
                        'VALUES (1, 1, 1)')
            conn.commit()

            M._m032_segment_channel_id(conn, cur)

            cols = {r[1] for r in cur.execute('PRAGMA table_info(recording_segments)')}
            self.assertIn('channel_id', cols)

            # Second run must be a no-op, not a duplicate-column error, and must not
            # re-run the backfill (harmless here, but this is the contract).
            M._m032_segment_channel_id(conn, cur)
            self.assertEqual(
                {r[1] for r in cur.execute('PRAGMA table_info(recording_segments)')}, cols)
            conn.close()

    def test_m032_backfills_non_group_recording_from_its_one_channel(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            cur.execute('INSERT INTO recordings (id, channel_id, group_id) VALUES (1, 7, NULL)')
            cur.execute('INSERT INTO recording_segments (id, recording_id, segment_number) '
                        'VALUES (1, 1, 1), (2, 1, 2)')
            conn.commit()

            M._m032_segment_channel_id(conn, cur)

            rows = cur.execute(
                'SELECT id, channel_id FROM recording_segments ORDER BY id').fetchall()
            self.assertEqual(rows, [(1, 7), (2, 7)],
                             'every segment of a non-group recording gets its one channel')
            conn.close()

    def test_m032_backfills_group_recording_by_correlating_failover_timestamps(self):
        """The real defect this migration exists for: a group recording's channel changes
        mid-recording, and only the events - not the segments - used to know it. Segment 1
        started before the failover and must be attributed to the original member; segment 2
        started after and must be attributed to the member it failed over to, even though
        Recording.channel_id only ever holds the final one."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            # Final member is channel 20 (what Recording.channel_id holds after failover) -
            # if the migration naively used that for every segment, segment 1 would be wrong.
            cur.execute('INSERT INTO recordings (id, channel_id, group_id) VALUES (1, 20, 3)')
            cur.execute("INSERT INTO recording_segments (id, recording_id, segment_number, started_at) "
                        "VALUES (1, 1, 1, '2026-01-01 00:00:05'), "
                        "(2, 1, 2, '2026-01-01 00:10:00')")
            cur.execute(
                "INSERT INTO recording_events (recording_id, timestamp, event_type, extra_data) "
                "VALUES (1, '2026-01-01 00:00:00', 'GROUP_MEMBER_SELECTED', '{\"channel_id\": 10}'), "
                "(1, '2026-01-01 00:05:00', 'GROUP_FAILOVER', '{\"to_channel_id\": 20}')")
            conn.commit()

            M._m032_segment_channel_id(conn, cur)

            rows = dict(cur.execute(
                'SELECT id, channel_id FROM recording_segments ORDER BY id').fetchall())
            self.assertEqual(rows[1], 10, 'segment 1 started before the failover - original member')
            self.assertEqual(rows[2], 20, 'segment 2 started after the failover - new member')
            conn.close()

    def test_m032_group_recording_with_no_events_falls_back_to_recording_channel(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            cur.execute('INSERT INTO recordings (id, channel_id, group_id) VALUES (1, 9, 3)')
            cur.execute('INSERT INTO recording_segments (id, recording_id, segment_number) '
                        'VALUES (1, 1, 1)')
            conn.commit()

            M._m032_segment_channel_id(conn, cur)

            self.assertEqual(
                cur.execute('SELECT channel_id FROM recording_segments WHERE id=1').fetchone(),
                (9,))
            conn.close()

    def test_m032_registered_at_its_own_version(self):
        registered = {v: fn for v, _d, fn in M.SCHEMA_MIGRATIONS}
        self.assertIs(registered.get(32), M._m032_segment_channel_id)
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, M.SCHEMA_MIGRATIONS[-1][0],
                         'CURRENT_SCHEMA_VERSION derives from the registry tail')
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, max(registered),
                         'SCHEMA_MIGRATIONS must stay in ascending version order')

    def test_m032_column_matches_the_model(self):
        # A create_all() DB never runs this step, so the two paths must not diverge.
        from app.database import RecordingSegment
        self.assertIn('channel_id', {c.name for c in RecordingSegment.__table__.columns})


class SyncLogSkipCountsMigrationTests(unittest.TestCase):
    """Migration 51 - account_sync_logs.skipped_malformed_urls/skipped_duplicate_stream_ids,
    backfilled from the MALFORMED_CHANNEL_URLS / DUPLICATE_STREAM_IDS_SKIPPED alert titles
    that were the counts' only record before (dev/changelog/926). Raw scratch-DB
    characterization like m032 above - the backfill is pure SQL."""

    COLS = ('skipped_malformed_urls', 'skipped_duplicate_stream_ids')

    def _make_scratch_db(self, td):
        import sqlite3
        conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
        cur = conn.cursor()
        cur.execute('CREATE TABLE accounts (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL)')
        cur.execute('CREATE TABLE account_sync_logs (id INTEGER PRIMARY KEY, '
                    'account_id INTEGER NOT NULL, started_at DATETIME NOT NULL, '
                    'completed_at DATETIME, status VARCHAR(32))')
        cur.execute('CREATE TABLE alerts (id INTEGER PRIMARY KEY, alert_type VARCHAR(64) NOT NULL, '
                    'severity VARCHAR(16) NOT NULL, title VARCHAR(255) NOT NULL, '
                    'created_at DATETIME NOT NULL)')
        conn.commit()
        return conn, cur

    @staticmethod
    def _log(cur, log_id, account_id, completed_at, status='SUCCESS'):
        cur.execute('INSERT INTO account_sync_logs (id, account_id, started_at, completed_at, '
                    'status) VALUES (?, ?, ?, ?, ?)',
                    (log_id, account_id, completed_at, completed_at, status))

    @staticmethod
    def _alert(cur, alert_type, title, created_at):
        cur.execute("INSERT INTO alerts (alert_type, severity, title, created_at) "
                    "VALUES (?, 'INFO', ?, ?)", (alert_type, title, created_at))

    def _counts(self, cur):
        return {r[0]: (r[1], r[2]) for r in cur.execute(
            f'SELECT id, {self.COLS[0]}, {self.COLS[1]} FROM account_sync_logs')}

    def test_m051_adds_both_columns_and_is_idempotent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            M._m051_sync_log_skip_counts(conn, cur)
            cols = {r[1] for r in cur.execute('PRAGMA table_info(account_sync_logs)')}
            for col in self.COLS:
                self.assertIn(col, cols)
            M._m051_sync_log_skip_counts(conn, cur)
            self.assertEqual(
                {r[1] for r in cur.execute('PRAGMA table_info(account_sync_logs)')}, cols)
            conn.close()

    def test_m051_backfills_each_count_from_the_alert_its_sync_raised(self):
        """The shape measured on the live database: the alert is created milliseconds after
        the sync row is stamped complete, and names the account rather than its id."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            cur.execute("INSERT INTO accounts (id, name) VALUES (1, 'provider-a'), "
                        "(2, 'IPTV: main')")
            self._log(cur, 10, 1, '2026-09-01 14:49:24.708780')
            self._log(cur, 20, 2, '2026-09-01 14:57:05.513902', status='PARTIAL')
            self._alert(cur, 'MALFORMED_CHANNEL_URLS',
                        'provider-a: skipped 250 malformed channel URL(s)',
                        '2026-09-01 14:49:24.720770')
            self._alert(cur, 'DUPLICATE_STREAM_IDS_SKIPPED',
                        'provider-a: skipped 332 duplicate stream ID(s)',
                        '2026-09-01 14:49:24.731000')
            # An account name carrying its own colon must still resolve to that account.
            self._alert(cur, 'DUPLICATE_STREAM_IDS_SKIPPED',
                        'IPTV: main: skipped 7 duplicate stream ID(s)',
                        '2026-09-01 14:57:05.524886')
            conn.commit()

            M._m051_sync_log_skip_counts(conn, cur)

            counts = self._counts(cur)
            self.assertEqual(counts[10], (250, 332))
            self.assertEqual(counts[20], (None, 7))
            conn.close()

    def test_m051_leaves_a_sync_with_no_matching_alert_untracked_not_zero(self):
        """No alert does not prove nothing was skipped: an ignored alert writes no row, and
        dismissed alerts are pruned after alerts.keep_days. Each case below must stay NULL."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            cur.execute("INSERT INTO accounts (id, name) VALUES (1, 'solo'), (2, 'twin'), "
                        "(3, 'twin')")
            self._log(cur, 1, 1, '2026-09-01 10:00:00')          # no alert at all
            self._log(cur, 2, 1, '2026-09-02 10:00:00')          # alert 2 minutes later
            self._alert(cur, 'MALFORMED_CHANNEL_URLS', 'solo: skipped 5 malformed channel URL(s)',
                        '2026-09-02 10:02:00')
            self._log(cur, 3, 1, '2026-09-03 10:00:00', status='ERROR')
            self._alert(cur, 'MALFORMED_CHANNEL_URLS', 'solo: skipped 6 malformed channel URL(s)',
                        '2026-09-03 10:00:00.010000')
            # Two accounts share the name, so the alert cannot say which one it was.
            self._log(cur, 4, 2, '2026-09-04 10:00:00')
            self._log(cur, 5, 3, '2026-09-04 10:00:00')
            self._alert(cur, 'MALFORMED_CHANNEL_URLS', 'twin: skipped 8 malformed channel URL(s)',
                        '2026-09-04 10:00:00.010000')
            conn.commit()

            M._m051_sync_log_skip_counts(conn, cur)

            for log_id, pair in self._counts(cur).items():
                self.assertEqual(pair, (None, None), f'sync log {log_id} must stay untracked')
            conn.close()

    def test_m051_resumes_a_backfill_interrupted_after_its_columns_were_added(self):
        """dev/changelog/686: the columns existing is not evidence the backfill ran. A crash
        between the ALTER and the backfill leaves exactly this state."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            M._register_backfill(conn, cur, M._BF_SYNC_LOG_SKIP_COUNTS)
            for col in self.COLS:
                cur.execute(f'ALTER TABLE account_sync_logs ADD COLUMN {col} INTEGER')
            cur.execute("INSERT INTO accounts (id, name) VALUES (1, 'acct')")
            self._log(cur, 1, 1, '2026-09-01 10:00:00')
            self._alert(cur, 'MALFORMED_CHANNEL_URLS', 'acct: skipped 3 malformed channel URL(s)',
                        '2026-09-01 10:00:00.010000')
            conn.commit()

            M._m051_sync_log_skip_counts(conn, cur)

            self.assertEqual(self._counts(cur)[1], (3, None))
            conn.close()

    def test_m051_does_not_rerun_a_finished_backfill(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            cur.execute("INSERT INTO accounts (id, name) VALUES (1, 'acct')")
            self._log(cur, 1, 1, '2026-09-01 10:00:00')
            self._alert(cur, 'MALFORMED_CHANNEL_URLS', 'acct: skipped 3 malformed channel URL(s)',
                        '2026-09-01 10:00:00.010000')
            conn.commit()
            M._m051_sync_log_skip_counts(conn, cur)

            cur.execute(f'UPDATE account_sync_logs SET {self.COLS[0]} = 99 WHERE id = 1')
            conn.commit()
            M._m051_sync_log_skip_counts(conn, cur)

            self.assertEqual(self._counts(cur)[1], (99, None))
            conn.close()

    def test_m051_registered_at_its_own_version(self):
        registered = {v: fn for v, _d, fn in M.SCHEMA_MIGRATIONS}
        self.assertIs(registered.get(51), M._m051_sync_log_skip_counts)
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, max(registered),
                         'SCHEMA_MIGRATIONS must stay in ascending version order')

    def test_m051_columns_match_the_model(self):
        # A create_all() DB never runs this step, so the two paths must not diverge.
        from app.database import AccountSyncLog
        model_cols = {c.name for c in AccountSyncLog.__table__.columns}
        for col in self.COLS:
            self.assertIn(col, model_cols)


class ProgramTitleBackfillDeterminismTests(unittest.TestCase):
    """Migration 2 - recordings.program_title/program_sub_title backfill
    (dev/docs/BUGS.md 2026-08-17 "m002 backfill can split title/sub_title across two rows").

    epg_entries has no uniqueness on (channel_id, start_time), so a channel with two entries
    at the same start_time gives the backfill's two correlated subqueries (one for title, one
    for sub_title) no guarantee they visit rows in the same order. Reproducing that without an
    artificial index would be flaky, so this seeds two indexes that individually cover title
    and sub_title - the same shape SQLite would pick between if either column ever gained its
    own covering index - so each subquery's LIMIT 1 is driven by a different sort order absent
    an explicit ORDER BY."""

    def _make_scratch_db(self, td):
        import sqlite3
        conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
        cur = conn.cursor()
        cur.execute('CREATE TABLE recordings (id INTEGER PRIMARY KEY, channel_id INTEGER, '
                    'program_start_time TEXT)')
        cur.execute('CREATE TABLE epg_entries (id INTEGER PRIMARY KEY, channel_id INTEGER, '
                    'start_time TEXT, title TEXT, sub_title TEXT)')
        # Individually cover title and sub_title so the two subqueries can be driven by
        # different tie-break orders - the mechanism that actually splits the two columns
        # across two different rows when no ORDER BY pins the tie-break to e.id.
        cur.execute('CREATE INDEX ix_cover_title ON epg_entries(channel_id, start_time, title)')
        cur.execute('CREATE INDEX ix_cover_sub ON epg_entries(channel_id, start_time, sub_title)')
        conn.commit()
        return conn, cur

    def _seed_duplicate_entries(self, conn, cur):
        # Same (channel_id, start_time); title and sub_title orderings deliberately disagree
        # (id=10 sorts first by title, id=20 sorts first by sub_title) so a tie-break that
        # isn't pinned to one column produces a different winner per subquery.
        cur.execute(
            "INSERT INTO epg_entries (id, channel_id, start_time, title, sub_title) VALUES "
            "(10, 1, '2026-01-01T00:00:00', 'AAA_TITLE', 'ZZZ_SUB'), "
            "(20, 1, '2026-01-01T00:00:00', 'ZZZ_TITLE', 'AAA_SUB')")
        cur.execute("INSERT INTO recordings (id, channel_id, program_start_time) "
                    "VALUES (1, 1, '2026-01-01T00:00:00')")
        conn.commit()

    def test_title_and_sub_title_come_from_the_same_entry(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            self._seed_duplicate_entries(conn, cur)

            M._m002_program_title(conn, cur)

            row = cur.execute(
                'SELECT program_title, program_sub_title FROM recordings WHERE id = 1'
            ).fetchone()
            self.assertEqual(row, ('AAA_TITLE', 'ZZZ_SUB'),
                             'title and sub_title must both come from entry id=10 (the lowest '
                             'id), not be split across id=10 and id=20')
            conn.close()

    def test_columns_are_added_and_step_stays_idempotent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            self._seed_duplicate_entries(conn, cur)

            M._m002_program_title(conn, cur)
            M._m002_program_title(conn, cur)  # must not raise on the already-added columns

            cols = {r[1] for r in cur.execute('PRAGMA table_info(recordings)')}
            self.assertIn('program_title', cols)
            self.assertIn('program_sub_title', cols)
            conn.close()


# Migration steps that reach into current application code, and why each is allowed to
# (app/migrations.py's module docstring carries the contract; dev/changelog/688).
#
# A step's source is normally the whole of what it does, which is what makes "never edit a
# shipped step" enforceable by reading one function. These calls break that: editing the
# callee edits the step's output for every database that has not crossed it yet. The point of
# enumerating them is that the list must not grow by accident - a new step importing app code
# has to arrive with a stated reason, not just a passing suite.
#
# Categories, in the order they cost a reader effort to reason about:
_DEFINED_BY = 'output is defined as whatever the current code produces'
_INPUT_STATE = 'reads config.yaml as input state, the same way a step reads the database'
_MODELS = 'ORM models/constants - a dropped column crashes the step rather than moving a value'
_PLUMBING = 'runner infrastructure, not part of any step\'s output'

_CURRENT_CODE_IMPORTS = {
    # The search indexes are defined as whatever search_index.py currently builds - an era
    # copy would stamp an index the running app cannot use. _ensure_search_index_shape is
    # what keeps that safe against an older-shaped chan_prog.
    ('_ensure_search_index_shape', '.search_index.SEARCH_INDEX_DDL'): _DEFINED_BY,
    ('_m021_search_indexes', '.search_index.SEARCH_INDEX_NAMES'): _DEFINED_BY,
    ('_m021_search_indexes', '.search_index.STATUS_OK'): _DEFINED_BY,
    ('_m021_search_indexes', '.search_index.rebuild_through_cursor'): _DEFINED_BY,
    ('_m022_search_index_watermark', '.search_index.SEARCH_INDEX_NAMES'): _DEFINED_BY,
    ('_m022_search_index_watermark', '.search_index.STATUS_OK'): _DEFINED_BY,
    ('_m022_search_index_watermark', '.search_index.rebuild_through_cursor'): _DEFINED_BY,
    ('_m023_chan_prog_description', '.search_index.SEARCH_INDEX_PROGRAMS'): _DEFINED_BY,
    ('_m023_chan_prog_description', '.search_index.STATUS_OK'): _DEFINED_BY,
    ('_m023_chan_prog_description', '.search_index.rebuild_through_cursor'): _DEFINED_BY,
    # channels.url_normalizable is defined as this function's answer - the column exists
    # only because deriving it per request was measured too expensive.
    ('_m024_channel_search_support', '.accounts.url_is_normalizable'): _DEFINED_BY,
    # _m004 is the one dependency of a different kind: legacy channel_testing keys are the
    # user's own config, which is state like the database, not code.
    ('_m004_unify_guide_health_checks', '.config.load_config'): _INPUT_STATE,
    # _m001's baseline backfills, the largest instance of the pattern in the file. Same
    # shape as _m024: each column is defined as what the current normalizer / duplicate
    # detector / scorer says, so freezing an era copy is the bug rather than the fix.
    ('_backfill_url_normalization', '.accounts.normalize_url'): _DEFINED_BY,
    ('_backfill_url_normalization', '.database.Account'): _MODELS,
    ('_backfill_url_normalization', '.database.Channel'): _MODELS,
    ('_backfill_duplicate_flags', '.accounts._recompute_duplicate_stream_urls'): _DEFINED_BY,
    ('_backfill_health_scores', '.health_score.blend_health_score'): _DEFINED_BY,
    ('_backfill_health_scores', '.health_score.score_test_quality'): _DEFINED_BY,
    ('_backfill_health_scores', '.config.load_config'): _INPUT_STATE,
    ('_backfill_health_scores', '.database.Channel'): _MODELS,
    ('_backfill_health_scores', '.database.ChannelTest'): _MODELS,
    ('_backfill_consecutive_test_failures', '.database.Channel'): _MODELS,
    ('_backfill_consecutive_test_failures', '.database.ChannelTest'): _MODELS,
    ('_backfill_consecutive_test_failures', '.database.TEST_STATUS_CANCELLED'): _MODELS,
    ('_backfill_consecutive_test_failures', '.database.TEST_STATUS_FAILED'): _MODELS,
    # Decides where the pre-migration snapshot lands. Runs before any step and stamps
    # nothing into the database it is backing up.
    ('_backup_before_migration', '.config.DEFAULT_DB_BACKUP_DIR'): _PLUMBING,
    ('_backup_before_migration', '.config.ensure_private_dir'): _PLUMBING,
    ('_backup_before_migration', '.config.resolve_app_path'): _PLUMBING,
    ('_backup_before_migration', '.tz_utils.get_display_tz'): _PLUMBING,
}


def _scan_current_code_imports():
    """Every (enclosing module-level function, dotted symbol) pair in app/migrations.py
    where a function body imports from the application package.

    Function-scoped by design: app/migrations.py's module-level `from . import db` is the
    session handle the whole runner is built on, not a step reaching for behavior.
    """
    import ast
    with open(M.__file__) as fh:
        source = fh.read()
    found = set()
    for node in ast.parse(source).body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.ImportFrom) and (sub.level or 0) > 0:
                module = '.' * sub.level + (sub.module or '')
                for alias in sub.names:
                    found.add((node.name, f'{module}.{alias.name}'))
    return found


class CurrentCodeDependencyTests(unittest.TestCase):
    """A shipped migration step whose output depends on current application code is allowed
    (module docstring), but the set of them is a contract, not an accident.

    This is a contract test, not a regression guard - no defect produced it. It fails when a
    new step reaches into app/ without a declared reason, which is the thing prose alone has
    repeatedly failed to prevent in this repo.
    """

    def test_scan_matches_the_declaration(self):
        self.assertEqual(
            _scan_current_code_imports(), set(_CURRENT_CODE_IMPORTS),
            'A migration step imports application code that is not declared in '
            '_CURRENT_CODE_IMPORTS (or a declared import was removed). Read '
            "app/migrations.py's module docstring - the step's output is now only "
            'reproducible if that callee never changes - then add the pair with the '
            'category that says why it is allowed.')

    def test_every_declared_category_is_from_the_vocabulary(self):
        vocabulary = {_DEFINED_BY, _INPUT_STATE, _MODELS, _PLUMBING}
        for key, category in _CURRENT_CODE_IMPORTS.items():
            self.assertIn(category, vocabulary, f'{key} carries an ad-hoc category')

    def test_every_declared_function_still_exists(self):
        for name, _symbol in _CURRENT_CODE_IMPORTS:
            self.assertTrue(hasattr(M, name),
                            f'_CURRENT_CODE_IMPORTS names {name}, which no longer exists')

    def test_registered_steps_reaching_for_app_code_are_declared(self):
        """The append-point specifically: a newly registered step is the likeliest way this
        list grows, and the likeliest one to be waved through."""
        declared = {name for name, _symbol in _CURRENT_CODE_IMPORTS}
        scanned = {name for name, _symbol in _scan_current_code_imports()}
        steps = {fn.__name__ for _v, _d, fn in M.SCHEMA_MIGRATIONS}
        self.assertEqual(sorted((scanned & steps) - declared), [],
                         'registered migration steps importing undeclared app code')


#: The six ix_ indexes migration 39 removes, and the one it deliberately leaves behind.
_M039_DROPPED = ('ix_epg_entries_start_time', 'ix_channels_account_id',
                 'ix_channel_events_channel_id', 'ix_channel_tests_channel_id',
                 'ix_channel_group_members_group_id',
                 'ix_ignored_alert_patterns_alert_type')
_M039_KEPT = 'ix_epg_entries_channel_id'


class PrefixRedundantIndexMigrationTests(unittest.TestCase):
    """Migration 39 - drop six single-column indexes a wider index or UNIQUE constraint
    already covers, so a migrated database matches what create_all() now builds
    (dev/changelog/692).

    A scratch-DB characterization: DROP INDEX IF EXISTS moves no row data, so there is no
    backfill half and nothing an interrupted run could leave half-done.
    """

    def _scratch(self, td):
        import sqlite3
        conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
        cur = conn.cursor()
        cur.execute('CREATE TABLE epg_entries (id INTEGER PRIMARY KEY, channel_id INTEGER, '
                    'start_time DATETIME, stop_time DATETIME)')
        cur.execute('CREATE TABLE channels (id INTEGER PRIMARY KEY, account_id INTEGER)')
        cur.execute('CREATE TABLE channel_events (id INTEGER PRIMARY KEY, channel_id INTEGER)')
        cur.execute('CREATE TABLE channel_tests (id INTEGER PRIMARY KEY, channel_id INTEGER)')
        cur.execute('CREATE TABLE channel_group_members (id INTEGER PRIMARY KEY, '
                    'group_id INTEGER)')
        cur.execute('CREATE TABLE ignored_alert_patterns (id INTEGER PRIMARY KEY, '
                    'alert_type VARCHAR(64))')
        for name, table, col in (
                ('ix_epg_entries_start_time', 'epg_entries', 'start_time'),
                ('ix_epg_entries_channel_id', 'epg_entries', 'channel_id'),
                ('ix_channels_account_id', 'channels', 'account_id'),
                ('ix_channel_events_channel_id', 'channel_events', 'channel_id'),
                ('ix_channel_tests_channel_id', 'channel_tests', 'channel_id'),
                ('ix_channel_group_members_group_id', 'channel_group_members', 'group_id'),
                ('ix_ignored_alert_patterns_alert_type', 'ignored_alert_patterns',
                 'alert_type')):
            cur.execute(f'CREATE INDEX {name} ON {table} ({col})')
        conn.commit()
        return conn, cur

    def _indexes(self, cur):
        return {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='index'")}

    def test_m039_drops_the_six_and_is_idempotent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._scratch(td)
            M._m039_drop_prefix_redundant_indexes(conn, cur)
            after = self._indexes(cur)
            for name in _M039_DROPPED:
                self.assertNotIn(name, after)

            # Second run must be a no-op, not a "no such index" error.
            M._m039_drop_prefix_redundant_indexes(conn, cur)
            self.assertEqual(self._indexes(cur), after)
            conn.close()

    def test_m039_leaves_the_measured_exemption_alone(self):
        """ix_epg_entries_channel_id is a prefix of ix_epg_entries_channel_stop and is kept
        anyway: without it SQLite builds an AUTOMATIC PARTIAL COVERING INDEX over 1.97M rows
        per query rather than using the wider one, 0.098s -> 2.914s on the production
        database. Dropping it here would be a 30x regression that no unit test can feel."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._scratch(td)
            M._m039_drop_prefix_redundant_indexes(conn, cur)
            self.assertIn(_M039_KEPT, self._indexes(cur))
            conn.close()

    def test_m039_survives_a_database_that_never_had_the_indexes(self):
        """A DB restored from a backup taken after this shipped, re-run from an older stamp."""
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn = sqlite3.connect(os.path.join(td, 'bare.db'))
            cur = conn.cursor()
            cur.execute('CREATE TABLE epg_entries (id INTEGER PRIMARY KEY)')
            conn.commit()
            M._m039_drop_prefix_redundant_indexes(conn, cur)
            conn.close()

    def test_the_models_no_longer_declare_any_of_the_six(self):
        """The other half of the parity rule: a fresh create_all() must not rebuild what
        this step removes, or migrated and fresh databases disagree again."""
        import importlib
        importlib.import_module('app.database')
        metadata = importlib.import_module('app').db.metadata
        declared = {idx.name for table in metadata.tables.values() for idx in table.indexes}
        for name in _M039_DROPPED:
            self.assertNotIn(name, declared,
                             f'{name} is dropped by migration 39 but still declared on the '
                             'models, so fresh installs would keep building it')
        self.assertIn(_M039_KEPT, declared,
                      f'{_M039_KEPT} is deliberately kept - a fresh install needs it too')

    def test_m039_registered_at_its_own_version(self):
        registered = {v: fn for v, _d, fn in M.SCHEMA_MIGRATIONS}
        self.assertIs(registered.get(39), M._m039_drop_prefix_redundant_indexes)
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, M.SCHEMA_MIGRATIONS[-1][0],
                         'CURRENT_SCHEMA_VERSION derives from the registry tail')
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, max(registered),
                         'SCHEMA_MIGRATIONS must stay in ascending version order')


class RetiredAlertDismissMigrationTests(unittest.TestCase):
    """Migration 52 - dismiss the open rows of the twelve types nothing raises any more
    (dev/changelog/928), so the counter starts at a number the user can act on. Raw
    scratch-DB characterization like m051 above - the migration is pure SQL."""

    def _make_scratch_db(self, td):
        import sqlite3
        conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
        cur = conn.cursor()
        cur.execute('CREATE TABLE alerts (id INTEGER PRIMARY KEY, '
                    'alert_type VARCHAR(64) NOT NULL, severity VARCHAR(16) NOT NULL, '
                    'title VARCHAR(255) NOT NULL, created_at DATETIME NOT NULL, '
                    'read_at DATETIME, dismissed_at DATETIME)')
        conn.commit()
        return conn, cur

    @staticmethod
    def _alert(cur, alert_type, dismissed_at=None):
        cur.execute("INSERT INTO alerts (alert_type, severity, title, created_at, "
                    "dismissed_at) VALUES (?, 'WARN', 'x', '2026-09-11 00:00:00', ?)",
                    (alert_type, dismissed_at))

    def _rows(self, cur):
        return {r[0]: (r[1], r[2]) for r in cur.execute(
            'SELECT alert_type, dismissed_at, read_at FROM alerts')}

    def test_m052_dismisses_open_retired_rows_and_leaves_live_types_alone(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            self._alert(cur, 'GROUP_FORMAT_MISMATCH')
            self._alert(cur, 'JOB_SKIPPED')
            self._alert(cur, 'CONVERSION_FAILED')
            conn.commit()

            M._m052_dismiss_retired_alert_type_rows(conn, cur)
            rows = self._rows(cur)

            self.assertIsNotNone(rows['GROUP_FORMAT_MISMATCH'][0])
            self.assertIsNotNone(rows['JOB_SKIPPED'][0])
            self.assertIsNone(rows['CONVERSION_FAILED'][0],
                              'a type still raised must keep its open alert')
            conn.close()

    def test_m052_does_not_mark_anything_read(self):
        """Dismissing is the action a user would have taken; claiming they read it is not."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            self._alert(cur, 'HEALTH_CHECK_COMPLETE')
            conn.commit()

            M._m052_dismiss_retired_alert_type_rows(conn, cur)

            self.assertIsNone(self._rows(cur)['HEALTH_CHECK_COMPLETE'][1])
            conn.close()

    def test_m052_is_rerunnable_and_does_not_move_an_existing_dismissal(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            self._alert(cur, 'SYNC_CHANNELS_NEW', dismissed_at='2026-01-01 00:00:00')
            self._alert(cur, 'SYNC_CHANNELS_MISSING')
            conn.commit()

            M._m052_dismiss_retired_alert_type_rows(conn, cur)
            first = self._rows(cur)
            M._m052_dismiss_retired_alert_type_rows(conn, cur)

            self.assertEqual(self._rows(cur), first,
                             'a re-run after an interrupted attempt must change nothing')
            self.assertEqual(first['SYNC_CHANNELS_NEW'][0], '2026-01-01 00:00:00',
                             'an already-dismissed row keeps its original timestamp')
            conn.close()

    def test_m052_covers_every_type_retired_when_it_shipped(self):
        """The migration spells its twelve names out rather than reading
        RETIRED_ALERT_TYPES, so that its effect stays what it was when it shipped.

        This is the tripwire for that choice: it fails if the two ever diverge. When they
        do because a thirteenth type has been retired, the fix is a NEW migration for that
        type's rows - never an edit to this one, which existing installs have already run
        and will never run again.
        """
        import tempfile
        from app.alerts import RETIRED_ALERT_TYPES
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            for alert_type in sorted(RETIRED_ALERT_TYPES):
                self._alert(cur, alert_type)
            conn.commit()

            M._m052_dismiss_retired_alert_type_rows(conn, cur)

            still_open = [t for t, (dismissed, _read) in self._rows(cur).items()
                          if dismissed is None]
            self.assertEqual(
                [], still_open,
                f'these types are retired but migration 52 does not dismiss them: '
                f'{still_open}. Add a new migration for them rather than editing 52.')
            conn.close()

    def test_m052_registered_at_its_own_version(self):
        registered = {v: fn for v, _d, fn in M.SCHEMA_MIGRATIONS}
        self.assertIs(registered.get(52), M._m052_dismiss_retired_alert_type_rows)
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, max(registered))


class StaleRecordingLinkMigrationTests(unittest.TestCase):
    """Migration 53 - unlink the alerts and pre-check tests still naming a recording they
    cannot be about (dev/changelog/929). Raw scratch-DB characterization like m052 above:
    the migration is pure SQL.

    The predicate that matters is the timestamp one. recordings.id is re-issued, so on the
    database this shipped against every stale row pointed at an id that existed again -
    a missing-row check alone would have repaired none of them.
    """

    REC_CREATED = '2026-09-11 10:26:53'

    def _make_scratch_db(self, td):
        import sqlite3
        conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
        cur = conn.cursor()
        cur.execute('CREATE TABLE recordings (id INTEGER PRIMARY KEY, created_at DATETIME)')
        cur.execute('CREATE TABLE alerts (id INTEGER PRIMARY KEY, '
                    'alert_type VARCHAR(64) NOT NULL, severity VARCHAR(16) NOT NULL, '
                    'title VARCHAR(255) NOT NULL, created_at DATETIME NOT NULL, '
                    'read_at DATETIME, dismissed_at DATETIME, recording_id INTEGER)')
        cur.execute('CREATE TABLE channel_tests (id INTEGER PRIMARY KEY, '
                    'test_started_at DATETIME NOT NULL, pre_check_recording_id INTEGER)')
        # Recording 17 is the id that was handed out a second time; 99 is a recording that
        # was never deleted, so the alerts naming it are genuinely about it.
        cur.execute('INSERT INTO recordings (id, created_at) VALUES (17, ?), (99, ?)',
                    (self.REC_CREATED, '2026-01-01 00:00:00'))
        conn.commit()
        return conn, cur

    @staticmethod
    def _alert(cur, alert_id, recording_id, created_at, dismissed_at=None):
        cur.execute("INSERT INTO alerts (id, alert_type, severity, title, created_at, "
                    "dismissed_at, recording_id) VALUES (?, 'LOG_ERROR', 'ERROR', 'x', "
                    "?, ?, ?)", (alert_id, created_at, dismissed_at, recording_id))

    def _alert_row(self, cur, alert_id):
        return cur.execute('SELECT recording_id, dismissed_at FROM alerts WHERE id = ?',
                           (alert_id,)).fetchone()

    def test_an_alert_older_than_the_recording_it_names_is_unlinked_and_dismissed(self):
        """The live case: the alert is about a deleted recording whose id was re-issued."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            self._alert(cur, 665, 17, '2026-09-10 20:21:04')
            conn.commit()

            M._m053_unlink_stale_recording_references(conn, cur)

            recording_id, dismissed_at = self._alert_row(cur, 665)
            self.assertIsNone(recording_id, 'the stale alert still deep-links to #17')
            self.assertIsNotNone(dismissed_at)
            conn.close()

    def test_an_alert_whose_recording_is_gone_is_unlinked_too(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            self._alert(cur, 700, 4242, '2026-09-10 20:21:04')
            conn.commit()

            M._m053_unlink_stale_recording_references(conn, cur)

            self.assertEqual(self._alert_row(cur, 700)[0], None)
            conn.close()

    def test_an_alert_that_really_is_about_its_recording_is_left_alone(self):
        """The migration must not detach the 15 healthy links on the way past."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            self._alert(cur, 701, 17, '2026-09-11 11:00:00')   # after #17 was created
            self._alert(cur, 702, 99, '2026-06-01 00:00:00')
            conn.commit()

            M._m053_unlink_stale_recording_references(conn, cur)

            self.assertEqual(self._alert_row(cur, 701), (17, None))
            self.assertEqual(self._alert_row(cur, 702), (99, None))
            conn.close()

    def test_a_stale_row_already_dismissed_keeps_its_original_timestamp(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            self._alert(cur, 664, 17, '2026-09-10 20:17:00',
                        dismissed_at='2026-09-11 22:51:33')
            conn.commit()

            M._m053_unlink_stale_recording_references(conn, cur)

            self.assertEqual(self._alert_row(cur, 664), (None, '2026-09-11 22:51:33'))
            conn.close()

    def test_m053_is_rerunnable(self):
        """Nulling the column is what takes a repaired row out of the predicate, so a
        retry after an interrupted run reaches only what the first attempt did not."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            self._alert(cur, 665, 17, '2026-09-10 20:21:04')
            self._alert(cur, 701, 17, '2026-09-11 11:00:00')
            conn.commit()

            M._m053_unlink_stale_recording_references(conn, cur)
            first = cur.execute(
                'SELECT id, recording_id, dismissed_at FROM alerts ORDER BY id').fetchall()
            M._m053_unlink_stale_recording_references(conn, cur)

            self.assertEqual(
                cur.execute('SELECT id, recording_id, dismissed_at FROM alerts '
                            'ORDER BY id').fetchall(), first,
                'a re-run after an interrupted attempt must change nothing')
            conn.close()

    def test_a_pre_check_test_older_than_its_recording_is_unlinked(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            cur.execute('INSERT INTO channel_tests (id, test_started_at, '
                        'pre_check_recording_id) VALUES (1, ?, 17), (2, ?, 17), (3, ?, 88)',
                        ('2026-09-10 20:00:00', '2026-09-11 12:00:00', '2026-09-11 12:00:00'))
            conn.commit()

            M._m053_unlink_stale_recording_references(conn, cur)

            rows = dict(cur.execute(
                'SELECT id, pre_check_recording_id FROM channel_tests').fetchall())
            self.assertIsNone(rows[1], 'a test that predates #17 cannot be about it')
            self.assertEqual(rows[2], 17, 'a genuine pre-check link was detached')
            self.assertIsNone(rows[3], 'the recording it named is gone')
            conn.close()

    def test_m053_registered_at_its_own_version(self):
        registered = {v: fn for v, _d, fn in M.SCHEMA_MIGRATIONS}
        self.assertIs(registered.get(53), M._m053_unlink_stale_recording_references)
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, max(registered),
                         'SCHEMA_MIGRATIONS must stay in ascending version order')


class GuideCheckRetargetAnnouncementMigrationTests(unittest.TestCase):
    """Migration 54 - remove what the one-time TV Guide retarget announcement left behind
    (dev/changelog/931): the alert it raised, and the user_prefs flag that gated the
    startup hook. Characterization of a new migration rather than a regression guard -
    there was no defect, the announcement was simply spent. Raw scratch-DB style like
    m052 above; the migration is pure SQL."""

    def _make_scratch_db(self, td):
        import sqlite3
        conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
        cur = conn.cursor()
        cur.execute('CREATE TABLE alerts (id INTEGER PRIMARY KEY, '
                    'alert_type VARCHAR(64) NOT NULL, severity VARCHAR(16) NOT NULL, '
                    'title VARCHAR(255) NOT NULL, created_at DATETIME NOT NULL, '
                    'read_at DATETIME, dismissed_at DATETIME)')
        cur.execute('CREATE TABLE user_prefs ("key" VARCHAR(64) NOT NULL PRIMARY KEY, '
                    'value TEXT)')
        conn.commit()
        return conn, cur

    @staticmethod
    def _alert(cur, alert_type):
        cur.execute("INSERT INTO alerts (alert_type, severity, title, created_at) "
                    "VALUES (?, 'INFO', 'x', '2026-08-19 17:56:46')", (alert_type,))

    def _state(self, cur):
        return (sorted(r[0] for r in cur.execute('SELECT alert_type FROM alerts')),
                sorted(r[0] for r in cur.execute('SELECT "key" FROM user_prefs')))

    def test_m054_removes_the_alert_and_the_flag_and_nothing_else(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            self._alert(cur, 'HEALTH_CHECK_TARGETS_CHANGED')
            self._alert(cur, 'HEALTH_CHECK_WINDOW')
            cur.execute("INSERT INTO user_prefs VALUES "
                        "('guide_check_retarget_announced', '2')")
            cur.execute("INSERT INTO user_prefs VALUES ('ch_logo_only', '1')")
            conn.commit()

            M._m054_drop_guide_check_retarget_announcement(conn, cur)

            self.assertEqual((['HEALTH_CHECK_WINDOW'], ['ch_logo_only']),
                             self._state(cur),
                             'only the retarget announcement leaves; a live alert type '
                             'and an unrelated preference both stay')
            conn.close()

    def test_m054_is_rerunnable(self):
        """Deletes by predicate, so a retry after an interrupted run is a no-op."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            self._alert(cur, 'HEALTH_CHECK_TARGETS_CHANGED')
            cur.execute("INSERT INTO user_prefs VALUES "
                        "('guide_check_retarget_announced', '2')")
            conn.commit()

            M._m054_drop_guide_check_retarget_announcement(conn, cur)
            first = self._state(cur)
            M._m054_drop_guide_check_retarget_announcement(conn, cur)

            self.assertEqual(([], []), first)
            self.assertEqual(first, self._state(cur))
            conn.close()

    def test_the_alert_type_is_gone_from_the_registry(self):
        """The rows are deleted rather than kept, so unlike a retired type this one keeps
        no label - and nothing may quietly reintroduce one."""
        from app.alerts import ALERT_TYPES
        self.assertNotIn('HEALTH_CHECK_TARGETS_CHANGED', ALERT_TYPES)

    def test_m054_registered_at_its_own_version(self):
        registered = {v: fn for v, _d, fn in M.SCHEMA_MIGRATIONS}
        self.assertIs(registered.get(54),
                      M._m054_drop_guide_check_retarget_announcement)


class RecordingsAutoincrementMigrationTests(unittest.TestCase):
    """Migration 55 - rebuild `recordings` with AUTOINCREMENT so SQLite stops re-issuing a
    deleted recording's id to the next recording created (dev/changelog/937).

    Raw scratch-DB style like m052/m054 above: the step is pure SQL over a table whose
    pre-55 shape is derived from the model here, so the fixture follows the model rather
    than freezing a column list that would drift the first time one is added.
    """

    #: A fresh build gets AUTOINCREMENT straight from the model, so the pre-55 shape has to
    #: be reconstructed by undoing exactly what the step does - which also keeps this
    #: fixture honest: if the two spellings ever stop being inverses, these tests say so.
    @staticmethod
    def _v54_recordings_ddl():
        from sqlalchemy.schema import CreateTable
        from sqlalchemy.dialects import sqlite as sqlite_dialect
        from app.database import Recording
        ddl = str(CreateTable(Recording.__table__).compile(
            dialect=sqlite_dialect.dialect())).strip()
        assert M._PK_COLUMN_NEW in ddl, (
            'Recording no longer compiles to the AUTOINCREMENT primary key this fixture '
            f'inverts - check __table_args__. Compiled:\n{ddl}')
        ddl = ddl.replace(M._PK_COLUMN_NEW, M._PK_COLUMN_OLD, 1)
        # The table constraint sits between the last column and the first FOREIGN KEY,
        # which is where SQLAlchemy puts it when it is not inlined on the column.
        first_fk = ddl.index('\n\tFOREIGN KEY')
        return ddl[:first_fk] + M._PK_TABLE_CONSTRAINT + ddl[first_fk:]

    def _make_scratch_db(self, td, rows=3):
        import sqlite3
        conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
        cur = conn.cursor()
        cur.execute(self._v54_recordings_ddl())
        # The two referencing tables that are not cascaded away with a recording. Their FK
        # clauses name `recordings`, which is what the drop-and-rename must leave intact.
        cur.execute('CREATE TABLE alerts (id INTEGER NOT NULL, alert_type VARCHAR(64) NOT NULL, '
                    'title VARCHAR(255) NOT NULL, recording_id INTEGER, PRIMARY KEY (id), '
                    'FOREIGN KEY(recording_id) REFERENCES recordings (id))')
        cur.execute('CREATE TABLE channel_tests (id INTEGER NOT NULL, '
                    'pre_check_recording_id INTEGER, PRIMARY KEY (id), '
                    'FOREIGN KEY(pre_check_recording_id) REFERENCES recordings (id))')
        for i in range(1, rows + 1):
            cur.execute(
                'INSERT INTO recordings (id, name, url, start_time, stop_time, status, '
                'final_file_size) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (i, f'rec {i}', f'http://example.test/live/{i}', '2026-09-01 00:00:00',
                 '2026-09-01 01:00:00', 'COMPLETED', i * 1000))
        cur.execute("INSERT INTO alerts (id, alert_type, title, recording_id) "
                    "VALUES (1, 'LOG_ERROR', 'move failed', ?)", (rows,))
        conn.commit()
        return conn, cur

    @staticmethod
    def _ddl(cur):
        return cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='recordings'"
        ).fetchone()[0]

    @staticmethod
    def _next_id(cur):
        """The id SQLite would hand the next recording, observed rather than predicted."""
        cur.execute("INSERT INTO recordings (name, url, start_time, stop_time, status) "
                    "VALUES ('next', 'u', '2026-09-01 00:00:00', '2026-09-01 01:00:00', "
                    "'SCHEDULED')")
        return cur.lastrowid

    def test_m055_rebuilds_with_autoincrement_and_is_idempotent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            self.assertNotIn('AUTOINCREMENT', self._ddl(cur),
                             'fixture must start in the pre-55 shape')

            M._m055_recordings_autoincrement(conn, cur)
            conn.commit()
            first = self._ddl(cur)
            self.assertIn('AUTOINCREMENT', first)
            self.assertNotIn('recordings_autoinc_new', {
                r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                'the temporary rebuild table was left behind')

            M._m055_recordings_autoincrement(conn, cur)
            conn.commit()
            self.assertEqual(first, self._ddl(cur), 'a second run rebuilt an AUTOINCREMENT table')
            conn.close()

    def test_m055_carries_every_row_and_column_value_across(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            before = cur.execute('SELECT * FROM recordings ORDER BY id').fetchall()
            cols_before = [d[0] for d in cur.description]

            M._m055_recordings_autoincrement(conn, cur)
            conn.commit()

            after = cur.execute('SELECT * FROM recordings ORDER BY id').fetchall()
            self.assertEqual([d[0] for d in cur.description], cols_before,
                             'the rebuilt table has a different column list')
            self.assertEqual(before, after, 'the rebuild did not preserve every row verbatim')
            conn.close()

    def test_m055_stops_a_deleted_id_being_re_issued(self):
        """The defect itself. Before the rebuild, deleting the newest recording hands its
        number to the next one; after it, the number is retired."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            cur.execute('DELETE FROM recordings WHERE id = 3')
            # Re-issued straight back, which is the defect. This also puts the table back
            # to ids 1-3, so the rebuild below starts from the shape the fixture describes.
            self.assertEqual(self._next_id(cur), 3,
                             'fixture must reproduce the reuse this migration closes')

            M._m055_recordings_autoincrement(conn, cur)
            conn.commit()

            cur.execute('DELETE FROM recordings WHERE id = 3')
            self.assertEqual(self._next_id(cur), 4,
                             'a deleted recording id was handed to the next recording')
            conn.close()

    def test_m055_retires_ids_from_the_highest_one_it_finds(self):
        """What the rebuild can and cannot promise, stated rather than assumed.

        The copy leaves sqlite_sequence at the highest id present, so every id up to and
        including that one is retired - including gaps left by recordings deleted from the
        middle. An id deleted from the TOP before the migration ran leaves no trace of
        itself anywhere (the referencing rows were unlinked by migration 53 and the
        cascaded ones are gone), so it is not knowable here and can be issued once more.
        That residual is what dev/changelog/929's unlink-on-delete already covers.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            cur.execute('DELETE FROM recordings WHERE id = 2')  # a gap below the maximum

            M._m055_recordings_autoincrement(conn, cur)
            conn.commit()

            self.assertEqual(
                cur.execute("SELECT seq FROM sqlite_sequence WHERE name='recordings'"
                            ).fetchone()[0], 3,
                'the rebuild did not carry the high-water mark across')
            self.assertEqual(self._next_id(cur), 4,
                             'a gap below the highest id was handed out again')
            conn.close()

    def test_m055_keeps_the_referencing_tables_pointing_at_recordings(self):
        """Foreign keys are off, so nothing errors if the drop/rename strands a referencing
        table - it would just silently stop describing anything."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            M._m055_recordings_autoincrement(conn, cur)
            conn.commit()

            for table, column in (('alerts', 'recording_id'),
                                  ('channel_tests', 'pre_check_recording_id')):
                targets = {(r[2], r[3]) for r in cur.execute(f'PRAGMA foreign_key_list({table})')}
                self.assertIn(('recordings', column), targets,
                              f'{table}.{column} stopped referencing recordings')
            self.assertEqual(cur.execute('PRAGMA foreign_key_check').fetchall(), [],
                             'the rebuild stranded a referencing row')
            conn.close()

    def test_m055_produces_the_same_ddl_a_fresh_build_does(self):
        """A migrated database and a create_all() one must not diverge - the model carries
        sqlite_autoincrement, and this step is the only other way to get there.

        Compared with the table name unquoted on both sides. ALTER TABLE ... RENAME TO
        rewrites the stored CREATE TABLE with the new name quoted, so a rebuilt table reads
        `CREATE TABLE "recordings"` where a fresh one reads `CREATE TABLE recordings`. That
        is SQLite's own spelling of an identifier and changes nothing about the table; it is
        also unavoidable, since the rename is what the rebuild is made of. Everything after
        the table name - every column, type, default and constraint - must match exactly.
        """
        import tempfile
        from sqlalchemy.schema import CreateTable
        from sqlalchemy.dialects import sqlite as sqlite_dialect
        from app.database import Recording
        fresh = str(CreateTable(Recording.__table__).compile(
            dialect=sqlite_dialect.dialect())).strip()
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            M._m055_recordings_autoincrement(conn, cur)
            conn.commit()
            rebuilt = self._ddl(cur).replace('CREATE TABLE "recordings"',
                                             'CREATE TABLE recordings', 1)
            self.assertEqual(rebuilt, fresh,
                             'the rebuilt table differs from what create_all() builds')
            conn.close()

    def test_m055_refuses_a_ddl_it_cannot_transform(self):
        """Loud, not silent. A rebuild that quietly produced a table without AUTOINCREMENT
        would leave the id still being re-issued while every presence check passed."""
        with self.assertRaises(RuntimeError) as ctx:
            M._recordings_autoincrement_ddl(
                'CREATE TABLE recordings (id INTEGER PRIMARY KEY, name TEXT)', 'tmp')
        self.assertIn('AUTOINCREMENT', str(ctx.exception))

    def test_m055_leaves_nothing_behind_if_it_dies_before_the_rename(self):
        """An interrupted rebuild must be re-runnable, which is what the step's explicit
        BEGIN buys.

        Python's sqlite3 driver opens a transaction for INSERT but not for DDL, so without
        it the temporary table's CREATE commits on its own and survives the rollback - and
        the retry then dies on "table already exists", aborting startup with no way past it
        short of dropping the table by hand. `recordings` itself survives either way,
        because the copy's implicit transaction covers the DROP.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._make_scratch_db(td)
            before = cur.execute('SELECT * FROM recordings ORDER BY id').fetchall()

            class _DiesOnRename:
                def __init__(self, inner):
                    self._inner = inner

                def __getattr__(self, name):
                    return getattr(self._inner, name)

                def execute(self, sql, *a):
                    if 'RENAME TO' in sql:
                        raise RuntimeError('simulated crash before the rename')
                    return self._inner.execute(sql, *a)

            with self.assertRaises(RuntimeError):
                M._m055_recordings_autoincrement(conn, _DiesOnRename(cur))
            conn.rollback()

            tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn('recordings', tables,
                          'a crash mid-rebuild destroyed the recordings table')
            self.assertNotIn('recordings_autoinc_new', tables,
                             'the rolled-back rebuild left its temporary table behind')
            self.assertEqual(cur.execute('SELECT * FROM recordings ORDER BY id').fetchall(),
                             before, 'the rolled-back rebuild lost rows')

            # The point of all of the above: the next startup finishes the job.
            M._m055_recordings_autoincrement(conn, cur)
            conn.commit()
            self.assertIn('AUTOINCREMENT', self._ddl(cur),
                          'an interrupted rebuild could not be re-run to completion')
            self.assertEqual(cur.execute('SELECT * FROM recordings ORDER BY id').fetchall(),
                             before, 'the retried rebuild lost rows')
            conn.close()

    def test_m055_registered_at_its_own_version(self):
        registered = {v: fn for v, _d, fn in M.SCHEMA_MIGRATIONS}
        self.assertIs(registered.get(55), M._m055_recordings_autoincrement)
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, M.SCHEMA_MIGRATIONS[-1][0],
                         'CURRENT_SCHEMA_VERSION derives from the registry tail')
        self.assertEqual(M.CURRENT_SCHEMA_VERSION, max(registered),
                         'SCHEMA_MIGRATIONS must stay in ascending version order')

    def test_the_model_carries_sqlite_autoincrement(self):
        """A fresh install never runs this step, so the model is what gets it there."""
        from app.database import Recording
        self.assertTrue(Recording.__table__.dialect_options['sqlite']['autoincrement'],
                        'Recording lost __table_args__ sqlite_autoincrement - a fresh '
                        'install would re-issue deleted recording ids again')


if __name__ == '__main__':
    unittest.main(verbosity=2)
