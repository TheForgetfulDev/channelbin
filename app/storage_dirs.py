"""Every directory ChannelBin writes into, what stops working without each one, and the
standing alert that says so.

The canonical list of configured write directories. Before this existed, the DVR output
directory and the move-on-complete destination were the only paths anything watched, and
only for reachability: a live-thumbnail folder the process could not create logged one
WARNING at startup and was otherwise silent for as long as it stayed broken, and the only
writability check at all was a line on the container's stdout that fired even for an
install that records somewhere else (dev/changelog/1009).

Product principle 1: a directory the user configured and the app cannot write to is a
stopped feature, so it reaches the Readiness card and the Alert Center, not only a log.
"""
import logging
import os
from collections import namedtuple

from .fs_utils import log_dir_outcome_change, probe_writable_dir

log = logging.getLogger(__name__)

#: A configured directory's role: what it is called, and what stops working while it is
#: unusable. The path alone says neither.
StorageRole = namedtuple('StorageRole', 'what consequence')

DVR_DIR_ROLE = StorageRole(
    'DVR output directory',
    'Recordings cannot be written while it is unusable, so any recording that starts '
    'now will fail immediately.')

MOVE_DEST_ROLE = StorageRole(
    'Move-on-complete destination',
    'Finished recordings cannot be filed there while it is unusable; they stay in the '
    'DVR output directory instead.')

LIVE_THUMBNAIL_ROLE = StorageRole(
    'Live thumbnail directory',
    'Recordings still capture, but no live or final-frame thumbnail can be saved, so '
    'their rows show a placeholder.')

POSTER_FRAME_ROLE = StorageRole(
    'Poster frame directory',
    'Recordings still capture, but no poster frame can be taken from inside the program, '
    'so their rows fall back to the final-frame thumbnail and a metadata file written for '
    'a media server carries that instead.')

SCREENSHOT_ROLE = StorageRole(
    'Health check screenshot directory',
    'Health checks still run, but none of their screenshots can be saved.')

CAPTURE_LOG_ROLE = StorageRole(
    'Capture log directory',
    "Recordings still capture, but ffmpeg's own error output is thrown away, so a "
    'segment that dies cannot say why.')

SCRATCH_ROLE = StorageRole(
    'Health check scratch directory',
    'Health checks cannot write their temporary capture clip, so they fail.')

PREVIEW_ROLE = StorageRole(
    'Live preview directory',
    'Channel previews cannot write their rolling segment window, so Preview fails to start.')

LOGO_CACHE_ROLE = StorageRole(
    'Logo cache directory',
    'Channel logos cannot be cached, so they keep loading from the provider.')

DB_BACKUP_ROLE = StorageRole(
    'Database backup directory',
    'No database snapshot can be written there, including the one taken before a schema '
    'upgrade.')

CONFIG_BACKUP_ROLE = StorageRole(
    'Config backup directory',
    'The daily config.yaml backup cannot be written there.')

PROFILE_POSTER_ROLE = StorageRole(
    'Profile poster directory',
    'A poster image cannot be uploaded to a recording profile, and recordings made under '
    'a profile that has one get a captured frame as their poster instead.')


#: The subfolders of recording.images_dir, one per kind of image (dev/changelog/1012). Each
#: kind is its own folder so a directory listing of one never has to skip the others - the
#: recordings list reads its thumbnail folder that way.
THUMBNAILS = 'thumbnails'
SCREENSHOTS = 'screenshots'
LOGOS = 'logos'
#: The poster images uploaded to recording profiles (app/profile_posters.py).
POSTERS = 'posters'
#: The frame taken from inside the program itself, a fixed offset after the program's own
#: start time (recorder.persist_poster_frame, dev/changelog/1060). Its own folder rather
#: than a second name inside THUMBNAILS, because the recordings list reads that folder as a
#: plain listing of "<recording id>.jpg" to decide which rows have an image.
POSTER_FRAMES = 'poster-frames'


def images_root(cfg: dict) -> str:
    """recording.images_dir, absolutized - the one reader of that key. Pass the config the
    caller already loaded."""
    from .config import config_default, resolve_app_path
    return resolve_app_path(cfg.get('recording', {}).get('images_dir')
                            or config_default('recording.images_dir'))


def image_dir(cfg: dict, kind: str) -> str:
    """The folder one kind of image is saved in: `kind` is THUMBNAILS, SCREENSHOTS, LOGOS
    or POSTERS."""
    return os.path.join(images_root(cfg), kind)


def configured_write_dirs(cfg: dict, capture_log_dir: str = None) -> list:
    """[(path, role)] for every directory this install will write into, in a fixed order.

    A directory belonging to a feature that is switched off is left out: an unwritable
    folder nobody will use is not a problem, and reporting one is the false alarm the
    container's old startup check raised. When two roles share one path the first wins,
    because the standing alert is keyed on the path.

    `capture_log_dir` is the caller's resolved app.config['CAPTURE_LOG_DIR'] - the one
    value here create_app() resolves rather than a config read - and is skipped when None.
    """
    from .config import db_backup_dir
    from .config_backup import get_backup_dir

    rec = cfg.get('recording', {})
    ct = cfg.get('channel_testing', {})
    candidates = [(rec.get('dvr_output_dir'), DVR_DIR_ROLE)]
    mc = rec.get('move_on_complete', {}) or {}
    if mc.get('enabled') and mc.get('destination'):
        candidates.append((mc['destination'], MOVE_DEST_ROLE))
    thumb = rec.get('live_thumbnail', {}) or {}
    if thumb.get('enabled', True):
        candidates.append((image_dir(cfg, THUMBNAILS), LIVE_THUMBNAIL_ROLE))
        # Same switch, because the poster frame is the other half of "keep an image of a
        # recording" and is captured in the same moment, from the same segment files.
        candidates.append((image_dir(cfg, POSTER_FRAMES), POSTER_FRAME_ROLE))
    if ct.get('screenshots_enabled', True):
        candidates.append((image_dir(cfg, SCREENSHOTS), SCREENSHOT_ROLE))
    if ct.get('capture_scratch_dir'):
        candidates.append((ct['capture_scratch_dir'], SCRATCH_ROLE))
    if (cfg.get('preview', {}) or {}).get('dir'):
        candidates.append((cfg['preview']['dir'], PREVIEW_ROLE))
    if capture_log_dir:
        candidates.append((capture_log_dir, CAPTURE_LOG_ROLE))
    if (rec.get('logo_cache', {}) or {}).get('enabled'):
        candidates.append((image_dir(cfg, LOGOS), LOGO_CACHE_ROLE))
    # Gated on the global sidecar switch: the poster only ever reaches a library through
    # the sidecar, and a profile can turn that on by itself, but the standing alert is for
    # an install that has opted in rather than one that never asked for the feature.
    if (rec.get('metadata_sidecar', {}) or {}).get('enabled'):
        candidates.append((image_dir(cfg, POSTERS), PROFILE_POSTER_ROLE))
    candidates.append((db_backup_dir(cfg), DB_BACKUP_ROLE))
    candidates.append((get_backup_dir(cfg), CONFIG_BACKUP_ROLE))

    seen = set()
    out = []
    for path, role in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        out.append((path, role))
    return out


def report_storage_path(path, probe, role):
    """Log the transition on `path` and move its standing alert along with it.

    Gated on log_dir_outcome_change's own transition memory, which is what keeps this
    free on the hot path: the sidebar's disk readout runs every 15s per open browser tab,
    and the alert must be one standing row per path rather than one row per poll.
    `role` None means an arbitrary path somebody asked about, which is logged only.
    """
    from .alerts import update_storage_path_alert
    what = role.what if role else 'Disk usage for'
    if log_dir_outcome_change(path, probe, what) and role is not None:
        update_storage_path_alert(path, probe, role.what, role.consequence)


def sweep_write_dirs(cfg: dict, capture_log_dir: str = None) -> list:
    """Probe every configured write directory and report each one. [(path, role, probe)].

    Every path gets the same writability probe the disk readout reports for the DVR
    directory, so the two reporters can never disagree about one path and flap its alert.
    """
    results = []
    for path, role in configured_write_dirs(cfg, capture_log_dir):
        probe = probe_writable_dir(path)
        report_storage_path(path, probe, role)
        results.append((path, role, probe))
    return results
