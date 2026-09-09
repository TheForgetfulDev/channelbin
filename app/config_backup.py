"""Config backup utilities: create, list, prune, diff, and apply backups."""
import difflib
import logging
import os
import shutil
from datetime import datetime, timedelta

from .tz_utils import get_display_tz, format_local

log = logging.getLogger(__name__)

_BACKUP_SUFFIX = '-channelbin-config-backup.yaml'


def _config_path() -> str:
    """The live config.yaml path, read from app.config at call time - never a module-
    level constant here. A constant computed once at import time can't see a test's
    ConfigSandbox/TestApp patch of app.config._CONFIG_PATH, which is exactly how this
    module used to copy the developer's real config.yaml into the real backup dir on
    every test-app build (dev/changelog/620)."""
    from . import config as cfgmod
    return cfgmod._CONFIG_PATH


def get_backup_dir(cfg: dict | None = None) -> str:
    """The configured config-backup directory, absolutized. The one place that reads
    this setting - callers that need the path (scheduler job, storage stats) use this
    rather than re-reading the key with their own default.

    Pass `cfg` when the caller already has a loaded config (e.g. create_app() at
    startup) - a bare load_config() call would re-read the real config.yaml and ignore
    any config_overrides a test passed in, escaping the test sandbox."""
    from .config import load_config, resolve_app_path, DEFAULT_CONFIG_BACKUP_DIR
    if cfg is None:
        cfg = load_config()
    return resolve_app_path(
        cfg.get('config_backup', {}).get('backup_dir', DEFAULT_CONFIG_BACKUP_DIR))


def do_backup(backup_dir: str | None = None, cfg_path: str | None = None) -> str:
    """Copy config.yaml into backup_dir with a timestamped filename. Returns the backup path."""
    from .config import ensure_private_dir, resolve_app_path
    if backup_dir is None:
        backup_dir = get_backup_dir()
    else:
        backup_dir = resolve_app_path(backup_dir)
    if cfg_path is None:
        cfg_path = _config_path()

    # A config backup is config.yaml verbatim, secrets included - 0700, never world-readable.
    ensure_private_dir(backup_dir)

    now_et = datetime.now(tz=get_display_tz())
    filename = now_et.strftime('%Y-%m-%d-%H-%M-%S') + _BACKUP_SUFFIX
    dest = os.path.join(backup_dir, filename)
    shutil.copy2(cfg_path, dest)
    log.info('Config backup created: %s', dest)
    return dest


def list_backups(backup_dir: str | None = None) -> list[dict]:
    """Return backup files sorted newest-first as list of {filename, path, created_at}."""
    from .config import resolve_app_path
    if backup_dir is None:
        backup_dir = get_backup_dir()
    else:
        backup_dir = resolve_app_path(backup_dir)

    if not os.path.isdir(backup_dir):
        return []

    results = []
    for fname in os.listdir(backup_dir):
        if not fname.endswith(_BACKUP_SUFFIX):
            continue
        path = os.path.join(backup_dir, fname)
        try:
            mtime = os.path.getmtime(path)
            created = datetime.fromtimestamp(mtime, tz=get_display_tz())
        except OSError:
            continue
        results.append({
            'filename': fname,
            'path': path,
            'created_at': format_local(created, 'iso_datetime_tz'),
            'created_ts': mtime,
        })

    results.sort(key=lambda x: x['created_ts'], reverse=True)
    return results


def prune_backups(backup_dir: str | None = None, keep_days: int | None = None):
    """Delete backup files older than keep_days."""
    if backup_dir is None:
        backup_dir = get_backup_dir()
    if keep_days is None:
        from .config import load_config
        keep_days = load_config().get('config_backup', {}).get('backup_retention_days', 14)

    if keep_days <= 0:
        return

    cutoff = datetime.now(tz=get_display_tz()) - timedelta(days=keep_days)
    for entry in list_backups(backup_dir):
        created = datetime.fromtimestamp(entry['created_ts'], tz=get_display_tz())
        if created < cutoff:
            try:
                os.remove(entry['path'])
                log.info('Pruned old config backup: %s', entry['filename'])
            except OSError as exc:
                log.warning('Could not prune backup %s: %s', entry['filename'], exc)


def get_diff(backup_path: str, cfg_path: str | None = None) -> list[str]:
    """Return unified diff lines showing what would change if backup is applied.
    - lines = removed from current; + lines = restored from backup."""
    if cfg_path is None:
        cfg_path = _config_path()

    with open(backup_path, 'r') as f:
        backup_lines = f.readlines()
    with open(cfg_path, 'r') as f:
        current_lines = f.readlines()

    diff = list(difflib.unified_diff(
        current_lines,
        backup_lines,
        fromfile='config.yaml (current)',
        tofile=os.path.basename(backup_path) + ' (backup)',
        lineterm='',
    ))
    return diff


def apply_backup(backup_path: str, cfg_path: str | None = None):
    """Overwrite config.yaml with the contents of backup_path.

    Goes through app/config.py's atomic replace under its write lock, like every other
    writer of config.yaml: a restore that truncates the live file and copies into it can
    be interrupted mid-copy, and a save landing between the copy and the migration below
    would be silently discarded by it (dev/changelog/671). The lock is an RLock precisely
    so migrate_config() can re-take it here."""
    from .config import (load_config, record_config_changes, migrate_config,
                         config_write_lock, _replace_config_file_from)

    live_cfg_path = _config_path()
    if cfg_path is None:
        cfg_path = live_cfg_path
    with config_write_lock:
        old = load_config()
        _replace_config_file_from(backup_path, cfg_path)
        if cfg_path == live_cfg_path:
            # A backup from an older config_version restores stale key names - re-run the
            # startup config migrations so the restored file self-heals immediately.
            migrate_config()
        new = load_config()
    log.info('Config rolled back from backup: %s', backup_path)
    record_config_changes(old, new)
