import os

from flask import Blueprint, jsonify, render_template, request

from ..config import load_config
from ..config_backup import get_backup_dir
from ..fs_utils import (DirProbe, PATH_MISSING, PATH_OK, PATH_UNREACHABLE,
                        classify_oserror, probe_dir, probe_writable_dir)
# Re-exported: the dashboard, the HA API and Readiness import the roles from here.
from ..storage_dirs import (DVR_DIR_ROLE, MOVE_DEST_ROLE, images_root,  # noqa: F401
                            report_storage_path)

system_bp = Blueprint('system', __name__)


def _dir_size(path):
    """Return total bytes used by all files directly in `path` (non-recursive for speed)."""
    if not path or not os.path.isdir(path):
        return None
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        return None
    return total


def _dir_size_recursive(path):
    """Return total bytes used by all files under `path` (recursive)."""
    if not path or not os.path.isdir(path):
        return None
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path):
            for fname in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, fname))
                except OSError:
                    pass
    except OSError:
        return None
    return total


def _disk_bytes(path, role=None):
    """(total, free) bytes for the filesystem holding `path`, or (None, None).

    The one statvfs in this file. Both the sidebar's rounded-GB readout and the
    Maintenance page's meter read it, so a mount that reports oddly reports the
    same way on both surfaces rather than in two spellings.

    (None, None) means "no honest answer available" - the surfaces draw no meter
    rather than a wrong one. It never means zero, and it is never another
    filesystem's numbers wearing this path's label.

    `role` is one of the app/storage_dirs.py StorageRole constants when `path` is a directory the
    user configured for storage, which is what earns it a standing alert on top of the
    log line. Omitted, this answers about an arbitrary path and stays a log line only.
    """
    if not path:
        return None, None
    # Walk up to the nearest existing ancestor (handles uncreated subdirs), but ONLY
    # past components that are genuinely absent - an uncreated subdir sits on the same
    # filesystem as its parent, so the parent's numbers answer the question. An
    # UNREACHABLE component does not: a mount point's parent is by definition a
    # different filesystem. A stale /dvr used to answer False to os.path.exists(), so
    # the walk climbed to / and reported the ROOT filesystem's numbers under the /dvr
    # label - a confidently wrong figure, which principle 1 ranks below no figure at
    # all (dev/changelog/723).
    candidate = path
    while True:
        probe = probe_dir(candidate)
        if probe.outcome == PATH_UNREACHABLE:
            report_storage_path(path, probe, role)
            return None, None
        if probe.outcome != PATH_MISSING:
            # OK, or present-but-not-a-directory/not-readable: statvfs answers about the
            # filesystem either way, and the try below catches it when it does not.
            break
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return None, None
        candidate = parent
    try:
        usage = os.statvfs(candidate)
    except OSError as exc:
        report_storage_path(path, classify_oserror(exc), role)
        return None, None
    # A configured directory is judged on whether it can be written to, not only on whether
    # it answers: that is the probe storage_dirs.sweep_write_dirs() reports for the same
    # path, and two reporters with different answers would flap one alert every poll.
    report_storage_path(path, probe_writable_dir(path) if role is not None
                        else DirProbe(PATH_OK, None, None), role)
    return usage.f_blocks * usage.f_frsize, usage.f_bavail * usage.f_frsize


def _disk_info(path, role=None):
    total, free = _disk_bytes(path, role)
    if total is None:
        return None
    used = total - free
    return {
        'path': path,
        'free_gb': round(free / 1073741824, 1),
        'used_gb': round(used / 1073741824, 1),
        'total_gb': round(total / 1073741824, 1),
        'percent_used': round(used / total * 100, 1) if total else 0,
    }


def _system_stats_dict():
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        mem_info = {
            'percent': round(mem.percent, 1),
            'used_gb': round(mem.used / 1073741824, 1),
            'total_gb': round(mem.total / 1073741824, 1),
        }
    except ImportError:
        cpu = None
        mem_info = None

    cfg = load_config()
    dvr_dir = cfg['recording']['dvr_output_dir']
    mc = cfg['recording'].get('move_on_complete', {})

    disk_dvr = _disk_info(dvr_dir, DVR_DIR_ROLE)

    disk_complete = None
    same_drive = True
    if mc.get('enabled') and mc.get('destination'):
        dest = mc['destination']
        # Probed unconditionally rather than behind os.path.exists(dest), which is the
        # error-swallowing predicate app/fs_utils.py exists to replace: a stale mount
        # answers False there, so the destination used to be skipped entirely - no
        # meter, no log line and no alert for the one storage path most likely to be a
        # NAS (dev/changelog/868). _disk_bytes() already walks up past a merely
        # uncreated destination to the filesystem it will be created on.
        disk_complete = _disk_info(dest, MOVE_DEST_ROLE)
        if disk_dvr and disk_complete:
            try:
                same_drive = os.stat(dvr_dir).st_dev == os.stat(dest).st_dev
            except OSError:
                same_drive = True

    return {
        'cpu_percent': cpu,
        'memory': mem_info,
        'disk_dvr': disk_dvr,
        'disk_complete': disk_complete if not same_drive else None,
        'same_drive': same_drive,
    }


@system_bp.route('/maintenance')
def maintenance():
    """The four operational panels that used to sit at the bottom of Settings.

    DESIGN.md 7 ruled them off that page and DESIGN.md 16 settles the page they
    landed on: named Maintenance, second to last in the nav, cards ordered
    Storage, Index, Backup, Service. Rollout: dev/changelog/444.

    Everything the page displays arrives by fetch; the only server-rendered data
    is the backup schedule, which is config rather than measurement.
    """
    cfg = load_config()
    backup_cfg = cfg.get('config_backup', {})
    return render_template(
        'maintenance.html',
        backup_hour=backup_cfg.get('backup_hour_et', 1),
        backup_enabled=backup_cfg.get('enabled', True),
        is_docker=bool(os.environ.get('CHANNELBIN_DOCKER')),
    )


@system_bp.route('/api/system/stats')
def system_stats():
    return jsonify(**_system_stats_dict())


@system_bp.route('/api/system/tools')
def system_tools():
    """The resolved ffmpeg/ffprobe and what that ffmpeg build includes, for the Maintenance
    page's External tools card.

    Deliberately its own endpoint rather than a few more keys on /api/system/stats above:
    that one is polled every 15 seconds by every open browser tab, and answering this
    question costs four process spawns. app/toolchain.py caches them process-wide, so this
    route is a dict read after the first call - but hanging it off a poll would still be
    writing down "spawn ffmpeg on a timer" as the design.
    """
    from ..toolchain import describe_capabilities, describe_tools, missing_tools
    tools = describe_tools()
    return jsonify(tools=tools, missing=missing_tools(tools),
                   capabilities=describe_capabilities(tools['ffmpeg']))


@system_bp.route('/api/system/storage-details')
def storage_details():
    cfg = load_config()
    db_path = cfg['database']['path']
    dvr_dir = cfg['recording']['dvr_output_dir']
    images_dir = images_root(cfg)
    backup_dir = get_backup_dir()

    db_bytes = None
    if db_path and os.path.isfile(db_path):
        try:
            db_bytes = os.path.getsize(db_path)
        except OSError:
            pass

    dvr_bytes = None
    dvr_file_count = None
    if dvr_dir and os.path.isdir(dvr_dir):
        total = 0
        count = 0
        try:
            with os.scandir(dvr_dir) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat().st_size
                            count += 1
                    except OSError:
                        pass
        except OSError:
            pass
        dvr_bytes = total
        dvr_file_count = count

    images_bytes = _dir_size_recursive(images_dir)
    backup_bytes = _dir_size(backup_dir)

    # What the directory totals above cannot say: how much room is left. Sent in
    # bytes rather than the sidebar's rounded GB so the meter and the "free of
    # total" line are the same number the rest of this payload is measured in.
    # Both are null when the path has no existing ancestor (an unmounted /dvr),
    # and the page draws no meter rather than a meter reading zero.
    disk_total, disk_free = _disk_bytes(dvr_dir, DVR_DIR_ROLE)

    return jsonify(
        db_bytes=db_bytes,
        db_path=db_path,
        dvr_dir=dvr_dir,
        dvr_bytes=dvr_bytes,
        dvr_file_count=dvr_file_count,
        images_dir=images_dir,
        images_bytes=images_bytes,
        backup_dir=backup_dir,
        backup_bytes=backup_bytes,
        disk_total=disk_total,
        disk_free=disk_free,
    )


@system_bp.route('/api/readiness')
def readiness_report():
    """Every readiness check, the capabilities behind them and the verdict.

    A GET that evaluates only the cheap checks: the three that cost a process, a provider
    connection or a real message report "not run yet" until somebody asks for them through
    the route below. That is the rule the whole feature is built on - nothing expensive
    happens because a page was opened (`dev/changelog/950`).
    """
    from ..readiness import evaluate
    return jsonify(success=True, **evaluate())


@system_bp.route('/api/readiness/run', methods=['POST'])
def readiness_run():
    """Run one on-demand check now, or every one that has not been asked for yet."""
    from ..readiness import CHECKS_BY_ID, ON_DEMAND, evaluate, pending_ondemand_ids, run_check
    data = request.get_json(silent=True) or {}
    check_id = data.get('check')
    if check_id is None:
        payload = None
        for pending in pending_ondemand_ids():
            payload = run_check(pending)
        return jsonify(success=True, **(payload or evaluate()))
    # A list or dict body value is unhashable; it is an unknown check, not a 500.
    check = CHECKS_BY_ID.get(check_id) if isinstance(check_id, str) else None
    if check is None:
        return jsonify({'error': f'Unknown check "{check_id}"'}), 404
    if check.cost != ON_DEMAND:
        return jsonify({'error': f'"{check.label}" runs on every load and is not asked for'}), 400
    return jsonify(success=True, **run_check(check_id))


@system_bp.route('/api/readiness/ignore', methods=['POST'])
def readiness_ignore():
    """Silence or un-silence one check.

    Checked here rather than in the template: a check the page draws no Ignore button for
    is still a route somebody can POST to, and CLAUDE.md puts enforcement server-side.
    """
    from ..readiness import evaluate, set_check_ignored
    data = request.get_json(silent=True) or {}
    check_id = data.get('check')
    ignored = bool(data.get('ignored'))
    if not isinstance(check_id, str):
        return jsonify({'error': f'Unknown check "{check_id}"'}), 404
    try:
        set_check_ignored(check_id, ignored)
    except KeyError:
        return jsonify({'error': f'Unknown check "{check_id}"'}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify(success=True, **evaluate())
