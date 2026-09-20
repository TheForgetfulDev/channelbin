import copy
import io
import logging
import os
import re
import shutil
import tempfile
import threading
import yaml as pyyaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

log = logging.getLogger(__name__)

# Round-trip YAML: preserves comments/key order on read so save_config() can merge new
# values into the existing file structure instead of re-serializing from scratch (which is
# what plain PyYAML dropped, and why it lost every hand-written comment - BUGS.md
# 2026-08-12). One shared instance, and ruamel's YAML() carries per-instance representer/
# serializer state, so a concurrent load and dump on it can corrupt either - every use of
# it, read and write alike, is therefore taken under config_write_lock below.
_yaml_rt = YAML(typ='rt')

_APP_ROOT = os.path.dirname(os.path.dirname(__file__))
_CONFIG_PATH = os.path.join(_APP_ROOT, 'config.yaml')

# config.yaml is the app's one shared mutable store outside the database, and it holds
# flask.secret_key and auth.password_hash - losing it locks the user out. Every writer
# takes this lock across its WHOLE read-merge-write unit, not just the final write: the
# same rule retry_on_locked applies to DB commits, for the same reason. Without it two
# concurrent saves each load, mutate and write, and the loser's field vanishes with no
# error anywhere - trivially reachable, since settings.js auto-saves per field against a
# threaded server. _parse_config_file() takes it too, so the shared _yaml_rt instance is
# never loading and dumping at the same time.
#
# RLock, not Lock: config_backup.apply_backup() holds it across a migrate_config() that
# takes it again, which a plain Lock would deadlock on.
config_write_lock = threading.RLock()

# Backup destinations default under the app's own instance/ dir, never /dvr: a config
# backup is config.yaml verbatim (flask.secret_key, notification webhook tokens) and a DB
# snapshot carries the accounts table's plaintext provider credentials, so both belong on
# local disk with private permissions rather than on a shared/network mount that every
# reader of the recordings share can also read (DESIGN-secrets.md §5).
DEFAULT_CONFIG_BACKUP_DIR = 'instance/config-backups'
DEFAULT_DB_BACKUP_DIR = 'instance/db-backups'

# Xtream dump/debug-replay files carry the account's plaintext username/password (every
# stream URL in a dumped playlist embeds them), so this default lives under instance/ for
# the same reason as the two backup dirs above - not dev/samples/, which is excluded from
# the shipped tree and would also leave dumps world-readable (dev/changelog/550).
DEFAULT_XTREAM_DUMP_DIR = 'instance/xtream-dumps'

# Claimed by app/scheduler.py::init_scheduler() to detect a second live process already
# running the scheduler/startup-recovery pass against the same database (dev/docs/BUGS.md
# 2026-08-14) - same instance/ home as the state above, and same reason tests need their
# own sandboxed path (tests/support/app.py overrides flask.pidfile_path per TestApp).
DEFAULT_PIDFILE_PATH = 'instance/channelbin.pid'

# Fields that require a service restart to take effect (running code only reads these at
# startup). Consulted by save_config()/record_config_changes() to decide whether a diffed
# change should raise the "restart required" flag.
RESTART_REQUIRED_KEYS = {
    'logging.level',
    'logging.file',
    'channel_testing.enabled',
    'channel_testing.schedule_hour',
    'channel_testing.test_days',
    'channel_testing.window.dispatch_interval_minutes',
    'config_backup.enabled',
    'config_backup.backup_hour_et',
    # flask.* and database.path are read-only in the GUI (behind_proxy renders in the
    # System card; serve_mockups is raw-YAML-only), but the config.yaml tab writes them
    # all the same - and each is read exactly once, inside create_app(), so an unflagged
    # write reports success and silently does nothing. behind_proxy wires ProxyFix and
    # serve_mockups registers a blueprint; neither is re-read per request
    # (dev/changelog/586).
    'flask.port',
    'flask.host',
    'flask.debug',
    'flask.secret_key',
    'flask.behind_proxy',
    'flask.serve_mockups',
    # Resolved into app.config['PIDFILE_PATH'] once at create_app() time; init_scheduler()
    # reads it from app.config, never a fresh load_config().
    'flask.pidfile_path',
    'database.path',
    # Applied by configure_sqlite_pragmas() when an engine is built, so existing
    # connections keep the old value until the process restarts.
    'database.cache_size_mb',
    'database.wal_size_limit_mb',
    # The engine's pool is built once by db.init_app(); Flask-SQLAlchemy's own docs say
    # config changes after that call are not reflected (dev/changelog/423's pool split).
    'database.pool_size',
    'database.max_overflow',
    'database.pool_timeout',
    'database.background_pool_size',
    'database.background_max_overflow',
    'database.background_pool_timeout',
    # Resolved into app.config['CAPTURE_LOG_DIR'] once at create_app() time; recorder.py
    # reads it from app.config, never a fresh load_config().
    'recording.capture_log_dir',
    # Both consumed once by _setup_logging()'s RotatingFileHandler construction.
    # logging.level/logging.file (above) already covered the same function; these two were
    # missed in the original pass.
    'logging.max_bytes',
    'logging.backup_count',
    # Becomes SESSION_COOKIE_SECURE, read once at create_app(). auth.enabled,
    # auth.password_hash and auth.session_timeout_minutes are deliberately NOT here -
    # app/auth.py::refresh_auth() re-reads app.config['AUTH'] on every write to those,
    # so they take effect immediately with no restart.
    'auth.cookie_secure',
    # display.nav_poll_interval_seconds is deliberately NOT here, despite looking like a
    # sibling of the create_app()-time keys above: it's read inside inject_globals(), a
    # @app.context_processor that calls load_config() fresh on every request, so it
    # already takes effect with no restart (dev/changelog/619).
}

# Leaf paths whose values should never be written to the log verbatim (secrets/tokens).
_SENSITIVE_EXACT_PATHS = {'flask.secret_key', 'auth.password_hash',
                          'integrations.home_assistant.api_key_hash'}

# One fixed mask string rendered by every settings READ surface for a sensitive leaf, and
# recognized by save_config() on WRITE to mean "keep the stored secret" (round-trip). ASCII
# only, never derived per-value (no length hints). Because it is restored on write, the
# literal string is unusable as a real secret value - clear a secret with '' instead.
MASK_SENTINEL = '********'


def _is_sensitive_path(path: str) -> bool:
    if path in _SENSITIVE_EXACT_PATHS:
        return True
    parts = path.split('.')
    # notifications.services.<name>.url - webhook URLs embed tokens/keys
    return len(parts) == 4 and parts[0] == 'notifications' and parts[1] == 'services' and parts[3] == 'url'


_DEFAULTS = {
    # Config schema version - bumped by CONFIG_MIGRATIONS (see migrate_config below).
    # Present in defaults so GUI/API saves always write the stamp back to config.yaml.
    'config_version': 1,
    'recording': {
        'dvr_output_dir': '/dvr',
        # One folder for every image the app saves, each kind in its own subfolder
        # (app/storage_dirs.py::image_dir): recording thumbnails, health check screenshots
        # and cached channel logos (dev/changelog/1012).
        'images_dir': '/dvr/images',
        # Where each capture ffmpeg's stderr is spooled while its segment runs, so the exit
        # code and the last thing ffmpeg said can be attributed to the segment that died
        # (dev/changelog/430). Files are tiny, per-segment, and unlinked at segment close.
        #
        # Deliberately NOT under dvr_output_dir: /dvr is a `soft` CIFS mount whose writes can
        # return EIO, and the diagnostic that explains a write failure must not live on the
        # filesystem that failed. Relative paths anchor to the app root via resolve_app_path().
        'capture_log_dir': 'capture-logs',
        'segment_duration_seconds': 0,
        # Auto-delete terminal recordings (COMPLETED/FAILED/ABORTED) once this many days
        # old. 0 = never delete. Overridable per Recording Profile
        # (RecordingProfile.retention_days). Swept by the daily recording_retention job.
        'retention_days': 0,
        # Whether the retention sweep also deletes the recording's file on disk, or only
        # its database row. Defaults to off (files kept) - deleting a user's recorded
        # video is the higher-cost mistake, so it requires an explicit opt-in rather than
        # being bundled into retention_days by default.
        'retention_delete_file': False,
        # {sub_title} renders the episode/segment name where the EPG supplies one, and
        # collapses out cleanly when it does not - render_filename_template() trims the
        # separator an empty substitution leaves behind.
        'filename_template': '{date} - {title} - {sub_title} - {channel}',
        # Tag names (see Tag/TagPattern in database.py) whose patterns get scrubbed from the
        # fully-rendered filename regardless of where they land - 'remove' deletes the
        # matched text, 'replace' swaps it for the tag's plain name. A tag name should only
        # ever appear in one of these two lists.
        'filename_tags_remove': [],
        'filename_tags_replace': [],
        'post_process': {
            'enabled': True,
            'format': 'mp4',        # 'mp4' or 'mkv'
            'delete_source': True,  # delete .ts after successful conversion
            # How long a conversion may run WITHOUT muxing its first output frame. It is
            # not a budget for the job: once output starts, stall_seconds below is the only
            # authority and the conversion runs as long as it keeps advancing
            # (dev/changelog/865). This bounds the one case stall detection deliberately
            # cannot see - ffmpeg seeking and analyzing a badly-damaged source, where
            # out_time legitimately sits at 0 for minutes.
            'pre_output_timeout_seconds': 1800,
            # Video re-encode policy for mp4 output (fixes Plex seek/FF/RW freezes caused by
            # timeline gaps captured during stream drops - see changelog 149):
            #   'damaged' - scan the .ts for timeline damage; re-encode only if found
            #   'always'  - re-encode every recording
            #   'never'   - always stream-copy video (fast, but damaged files seek poorly)
            'reencode_mode': 'damaged',
            # Compression tuning for mp4 output. video_crf only applies when a video
            # re-encode actually happens (reencode_mode 'always', or 'damaged' finding
            # damage) - the plain stream-copy path ignores it. audio_bitrate_kbps applies to
            # every mp4 conversion, re-encoded or not, since AAC audio is always re-encoded
            # from ADTS to raw AAC for mp4 output regardless of the video path.
            'video_crf': 20,            # libx264 CRF, 0-51, lower = better quality/larger file
            'audio_bitrate_kbps': 192,  # AAC bitrate for mp4 output
            # Conversion resilience (app/postprocessor.py supervised runner). A conversion
            # can be killed mid-flight (ffmpeg crash, a service restart, a stall); these
            # control whether the app restarts it and how it detects a stall.
            'auto_restart': True,             # restart a conversion that dies/stalls
            'max_restart_attempts': 3,        # give up after this many restarts; 0 disables auto-restart
            # The only bound on a conversion that has started producing output. 0 disables
            # it, which leaves no liveness signal at all - so pre_output_timeout_seconds
            # above reverts to a whole-job wall clock in that case rather than leaving a
            # hung conversion to run forever.
            'stall_seconds': 300,             # kill+count a conversion whose output stops advancing; 0 disables
            'progress_interval_seconds': 5,   # how often ffmpeg writes -progress and we poll/publish
            # mp4 conversion vs. an imminent/active recording - local CPU/disk contention,
            # not covered by DESIGN-concurrency.md's tester/sync/recorder precedence doctrine
            # (conversion isn't one of that doc's four actors). 'off' = no collision
            # avoidance. 'cancel' = the conversion yields: it SIGSTOPs its own ffmpeg and
            # continues once clear, so the recording is never delayed (recordings always
            # win). 'wait' = the opposite - a recording whose start_time arrives while a
            # conversion is running waits for it to finish, UNLESS its own stop_time would
            # already have passed by then, in which case it is marked FAILED with a loud
            # alert instead of silently missed.
            'collision_policy': 'cancel',
            # Lookahead window, in units of the source the conversion still has LEFT to
            # encode: window_seconds = remaining / this multiplier. >= 0.1, no upper bound.
            # 1.0 assumes the conversion runs at 1x realtime (always safe, the default);
            # 2.0 assumes 2x realtime and only needs to look ahead half of it. Before the
            # conversion starts the remainder is the whole duration; it shrinks from there,
            # so a nearly-finished job steps aside for almost nothing (dev/changelog/953).
            'collision_lookahead_multiplier': 1.0,
        },
        'move_on_complete': {
            'enabled': False,
            'destination': '',      # absolute path to destination folder
        },
        'post_script': {
            # A post-script runs an arbitrary executable as a subprocess after every
            # recording (app/postprocessor.py) - a code-execution setting, so it ships
            # OFF with a blank path. Someone who never configured it must not have it run.
            'enabled': False,
            'path': '',
            'timeout_seconds': 300,
        },
        # The .nfo file and poster written beside a finished recording so Plex, Jellyfin,
        # Emby and Kodi can describe it from local disk instead of parsing the filename
        # (app/metadata_sidecar.py, dev/changelog/1057). Off by default: it writes files
        # into the user's library folder, which nobody who did not ask for it should find
        # there. Overridable per recording profile via
        # RecordingProfile.metadata_sidecar_enabled.
        'metadata_sidecar': {
            'enabled': False,
        },
        # Every image ChannelBin keeps OF a recording, not only the live one: the block
        # also governs the final-frame thumbnail and the poster frame below. Named for the
        # first of the three and left that way deliberately - renaming a config block costs
        # every install a migration to answer a question nobody asked.
        'live_thumbnail': {
            'enabled': True,
            'min_regen_interval_seconds': 10,
            'capture_timeout_seconds': 12,
            'auto_refresh_seconds': 60,   # frontend polling cadence; user-configurable in Settings
            # How far after the program's OWN start time the poster frame is taken
            # (dev/changelog/1060). Anchored there rather than on the recording's start so
            # front padding does not put a countdown clock or the previous show on the
            # cover. 0 means the program's first moment.
            'poster_frame_offset_seconds': 60,
            # Which of the two images a FINISHED recording shows: 'poster' (the frame from
            # inside the program) or 'last_frame' (the final-frame thumbnail). Either way
            # the other one is served when the chosen one does not exist, so a recording
            # made before this shipped still shows something. An IN_PROGRESS recording is
            # not covered - it has no poster frame yet and its thumbnail is regenerated
            # live, which is the whole point of that one.
            'finished_image': 'poster',
        },
        # Local caching of Channel.logo_url images instead of hotlinking the provider/CDN
        # on every page view (dev/changelog/601).
        # Off by default: an install with no need for it shouldn't get a background job and
        # a growing cache directory it never asked for. The scheduled job (app/logo_cache.py,
        # app/scheduler.py::_logo_cache_job) only fetches logos for channels that are in the
        # TV Guide or belong to a channel/health-check group - never the whole catalog.
        # A deliberate scope choice, not an oversight.
        'logo_cache': {
            'enabled': False,
        },
        'gather_health_data': True,  # run ffprobe on completed .ts to capture resolution/fps/frames
        # If true, only one concat+conversion job runs at a time, and none run while
        # any recording is IN_PROGRESS - a queued concat waits rather than failing.
        # Local CPU/disk contention control; unrelated to accounts.default_max_connections.
        'serialize_concat': False,
    },
    'watchdog': {
        'poll_interval_seconds': 5,
        'stall_timeout_seconds': 30,
        'restart_delay_seconds': 30,
        'max_consecutive_failures': 10,
        # Dead-stream fast-fail: a stream that reconnects but immediately dies again
        # (identical low byte counts each time) never trips max_consecutive_failures
        # because each restart looks "successful". This is an independent trip-wire.
        'early_fail_window_seconds': 60,          # segment must stall within this long to be a candidate
        'early_fail_min_bytes': 1048576,          # ...and produce fewer than this many bytes (1MB)
        'early_fail_abort_count': 3,               # consecutive early-failure segments before auto-abort
        'early_fail_abort_window_seconds': 180,    # the streak must occur within this window
        # Dead-stream retry (Product Principle 2): once the fast-fail trip-wire above fires,
        # retry at 1/2/5/15 minutes then hourly (hardcoded cadence) instead of giving up
        # immediately, up to this many total attempts - whichever comes first against the
        # recording's own scheduled window. 0 = no retries, same as pre-retry behavior.
        'dead_stream_max_retry_attempts': 10,
        # Stall-rate demotion: move a group-backed recording off a member that stalls this
        # many times inside a rolling window, even though every restart succeeds. The three
        # trip-wires above are all "the feed is dead" shaped and a feed that always comes
        # back reaches none of them - the successful restart zeroes the very counter that
        # would trip max_consecutive_failures. 0 = never move on stall rate.
        #
        # A WIDER window is a LOOSER trigger, not a stricter one: the count is the same and
        # there is more time to reach it. Measured on recording 14's 27 stalls, 3-in-30
        # first fires at +24.2 min and 3-in-10 not until +65.0 (dev/changelog/889).
        'stall_move_count': 3,
        'stall_move_window_minutes': 30,
        # Provider placeholder detection: a segment holding more than this many seconds of
        # content per second of wall clock, which ALSO ended on a clean EOF (ffmpeg exited 0
        # by itself), is the provider's finite "channel offline" clip rather than the
        # channel, and is discarded instead of joined into the final file.
        #
        # Detection is the ratio and never the byte count - a different provider's clip is a
        # different size, and the app must not be tuned to one of them. Measured on the
        # recordings that prompted this: real placeholders ran 86x-150x; the worst legitimate
        # short segment is a 13-29s buffer replay, which on a 5s segment is about 6x; the
        # fastest sustained real delivery ever seen is 3.76x. The clean-EOF condition is
        # load-bearing rather than decoration - a live feed never ends in EOF at 0 - because
        # the ratio alone would misfire on a short buffer-replay segment (dev/changelog/957).
        # 0 disables the detector entirely.
        'placeholder_content_ratio': 10,
        # Frozen/looping feed detection: a provider that re-serves the same few seconds
        # forever still writes bytes at full rate and still advances ffmpeg's frame
        # counter, so nothing above sees it. The signal that does is content time per
        # second of wall clock, sustained - a feed delivering faster than real time for
        # this long is not a feed (dev/changelog/964).
        #
        # The gap the thresholds sit in was measured, not chosen: real segments of 60s or
        # longer ran a median 1.03x and a maximum 1.29x, while the frozen feed ran 3.76x
        # for 2h18m. The WINDOW is what makes 1.5x safe, and it is why the window is not
        # shorter: the per-connect back-buffer is a fixed 13-29s of content, so it is a
        # CONSTANT rather than a rate and its ratio decays as the window lengthens. The
        # worst measured one reads 1.48x across 60s - two hundredths under the trigger -
        # and 1.24x across 120s. Seconds of WALL CLOCK here, not of content.
        #
        # Only meaningful while -re is off, which is what an unbounded capture gets today.
        # Set recording.segment_duration_seconds and ffmpeg paces at 1x, a fast provider
        # just fills a socket buffer, and this detector goes blind. 0 disables it.
        'fast_delivery_ratio': 1.5,
        'fast_delivery_window_seconds': 120,
        # Strikes on one member before the recording moves off it - the same "retry a few
        # times, then move on" the other trip-wires use, because a frozen feed has a
        # decent chance of resuming on a reconnect. 0 moves on at the first detection.
        'fast_delivery_strike_count': 3,
        # The post-capture half of the same question, and the one knob here the watchdog
        # does not read: app/concatenator.py applies it to each finished segment just
        # before the join, to FLAG a recording whose video arrived faster than the clock
        # without stopping anything. A segment under the live thresholds above is not
        # proven bad - it is only unexplained - so it is kept, labelled, and left for a
        # human to judge before they sit down to watch it (dev/changelog/966).
        #
        # SURPLUS SECONDS, not a ratio, and the shape is the measurement rather than a
        # preference. The per-connect back-buffer is a fixed 13-29s of content, so it is a
        # CONSTANT: a constant threshold separates it at every segment length, while a
        # ratio's sensitivity drifts with length in both directions at once. Measured over
        # every segment in this app's database carrying a content duration, the worst
        # honest surplus is +40.1s (on a 47-minute segment reading 1.01x) and nothing sits
        # between there and the placeholder clips at +594s; meanwhile two ordinary 15s
        # back-buffer segments read 2.43x and 1.60x, and an 8-hour segment running 10%
        # fast would be +48 minutes of suspect video at only 1.10x. 120 is three times
        # clear of the worst honest case and needs no minimum-length gate to stay there.
        #
        # Raise it if in-process reconnects make it noisy: since dev/changelog/958 a
        # segment survives a silent socket without ending, so one long segment can collect
        # several back-buffers where it used to collect one. Its own DIAGNOSTICS detail
        # reports how many times it reconnected. 0 disables the flag.
        'fast_delivery_surplus_seconds': 120,
    },
    'ffmpeg': {
        'path': 'ffmpeg',
        # Empty means "follow ffmpeg" - the ffprobe beside an ffmpeg.path that carries a
        # directory, else PATH. Set it only for a toolchain whose halves genuinely live
        # apart; see describe_ffprobe_resolution().
        'ffprobe_path': '',
        'extra_input_args': [],
        'extra_output_args': [],
        # Seconds any single read off an http(s) stream may block before ffmpeg gives up on
        # the connection and reconnects. Without it a provider that stops sending but leaves
        # the socket open blocks ffmpeg's read forever, which costs a SIGKILL and a whole new
        # segment for every stall - see the comment at the flag in proc_utils.build_capture_cmd
        # for what was measured (dev/changelog/958). 0 disables it.
        #
        # Keep it comfortably BELOW watchdog.stall_timeout_seconds or it is inert: the
        # watchdog kills the process at that point regardless, so a read timeout at or above
        # it never gets to fire. report_read_timeout_state() says so at startup and on save.
        #
        # 20 rather than the original 5: a provider delivering 5-second chunks waits about
        # that long between reads at the live edge, and 5 reconnected on those gaps, each
        # reconnect replaying the provider's buffer (dev/changelog/998).
        'read_timeout_seconds': 20,
        # Whether a recording reads the stream at real-time speed (-re) by default. Each
        # channel may override it (Channel.pace_realtime). Off by default: on a steady feed it
        # changes nothing measurable (dev/changelog/437). On a provider that sends a burst of
        # buffered video and then trickles, an unpaced read drains the burst, waits at the
        # live edge long enough for read_timeout_seconds to fire, and each reconnect is
        # answered with the same buffer again - pacing keeps the read behind the live edge
        # the way a player does (dev/changelog/997). A bounded segment
        # (recording.segment_duration_seconds) is always paced whatever this says.
        'pace_realtime': False,
        # The concat joins however many bytes the capture produced, so its size is not
        # knowable in advance and no whole-job deadline can be honest about it - the fixed
        # budget these replaced killed a 42.6 GB join at roughly the halfway mark while it
        # was writing 71 MB/s, near line rate for the mount (dev/changelog/947). Same two
        # rules as the conversion, for the same reason (post_process above): a bound on the
        # phase before ffmpeg writes anything, and after that a no-growth stall budget as
        # the sole authority. A JOIN THAT IS STILL WRITING IS NEVER KILLED.
        'concat_pre_output_timeout_seconds': 300,
        # The only bound on a concat that has started writing - it watches bytes landing in
        # the output file, which is the whole job of a stream copy. 0 disables it, which
        # leaves no liveness signal at all, so concat_pre_output_timeout_seconds above
        # reverts to a whole-job wall clock in that case rather than letting a hung ffmpeg
        # run forever.
        'concat_stall_seconds': 300,
    },
    'flask': {
        'port': 5000,
        'host': '0.0.0.0',
        'debug': False,
        'secret_key': 'change-me-in-production',
        # True = trust X-Forwarded-For/-Proto/-Host from a reverse proxy (ProxyFix).
        # Must stay False when no proxy fronts the app - otherwise any client can
        # spoof its own scheme/host/IP via those headers.
        'behind_proxy': False,
        # True = serve the gitignored dev/mockups/ folder at /mockups/ (a dev-only
        # convenience for reviewing static UI mockups in the browser). Stays False on
        # any shared/proxied deploy; the route is not registered at all when False.
        'serve_mockups': False,
        # None → DEFAULT_PIDFILE_PATH (instance/channelbin.pid). Raw-YAML-only, same as
        # serve_mockups - not something a normal install ever needs to change.
        'pidfile_path': None,
    },
    # A door on something that was standing open, not a hardened auth system: one shared
    # password, no usernames/accounts/roles/2FA. Off by default so existing installs are
    # unaffected. See app/auth.py and dev/docs/DESIGN-secrets.md §6.
    'auth': {
        'enabled': False,
        'password_hash': '',              # werkzeug scrypt hash - NEVER the plaintext
        'session_timeout_minutes': 0,      # 0 = stay logged in indefinitely on that device
        'cookie_secure': False,            # set true only when always served over HTTPS
    },
    # Inbound-facing integrations that poll ChannelBin's own state (as opposed to
    # notifications.services.<name>, which is ChannelBin pushing OUT to a service - the two
    # are unrelated despite both naming 'home_assistant'). Off by default; see app/routes/ha.py.
    'integrations': {
        'home_assistant': {
            'enabled': False,
            'api_key_hash': '',           # werkzeug scrypt hash - NEVER the plaintext
        },
    },
    'database': {
        'path': os.path.join(_APP_ROOT, 'dvr.db'),
        # Pre-migration DB snapshots (app/migrations.py): before any pending schema
        # migration runs at startup, the DB is copied via VACUUM INTO. This is the whole
        # rollback story - there are no down-migrations.
        'pre_migration_backup': True,
        'backup_dir': DEFAULT_DB_BACKUP_DIR,
        'migration_backups_keep': 3,   # 0 = keep all
        # SQLite page cache per connection, in MB (app/db_utils.py converts it to the
        # pragma's negative-KiB form). SQLite's own default is 2MB, which is not a
        # considered figure for a database this size - see dev/changelog/363.
        'cache_size_mb': 64,

        # How much of dvr.db-wal SQLite is allowed to KEEP once it no longer needs it
        # (PRAGMA journal_size_limit, in MB). Read the next paragraph before tuning this.
        #
        # IT IS NOT A QUOTA AND MUST NEVER BE TREATED AS ONE. A transaction always grows the
        # WAL to whatever it needs and always succeeds - measured on this box at 20x and 32x
        # this setting, both committing fine (dev/changelog/424). The limit only takes effect
        # on the first commit AFTER a checkpoint has rewound the WAL, and all it does then is
        # give the unused tail back to the filesystem. So a user with 20 accounts whose sync
        # legitimately needs a 900MB WAL gets a 900MB WAL; they just do not keep it forever.
        # Set it too LOW and a workload that routinely exceeds it pays truncate-then-re-extend
        # churn on every cycle; a WAL that stays under the limit is never touched at all.
        #
        # Before this existed the value was SQLite's default of -1, "never truncate", and
        # dvr.db-wal sat at 2758.7MB against a 1598.6MB database with only 1.2MB of it live -
        # the high-water mark of one 16-minute incident, kept forever. The failure that
        # matters is not the size, it is that nothing bounded it: this box had 41GB free, a
        # Raspberry Pi on a 32GB card would have filled up.
        #
        # 256 is ~64x the 4MB autocheckpoint threshold, so ordinary operation never approaches
        # it and nothing is ever truncated; it is a ceiling for the pathological case, not a
        # working size. 0 means no limit (SQLite's -1), i.e. the old behavior, for anyone who
        # wants it back.
        'wal_size_limit_mb': 256,

        # TWO CONNECTION POOLS on one dvr.db, and the split is the point: the UI pool serves
        # requests, the background pool serves everything with no request behind it - the
        # account sync, the search index rebuild, every scheduled job, the recorder threads.
        # Drawing from separate sets is what makes "browsing cannot starve a recording or a
        # sync of its connection" structural rather than lucky. Until 2026-08-01 there was one
        # pool and no setting at all, so SQLAlchemy's 5 + 10 default applied by accident, and
        # search scans holding every connection killed an account sync outright
        # (dev/changelog/423). Restart to apply - engines are built once, at startup.
        #
        # THE CEILING COSTS MEMORY. cache_size_mb above is per CONNECTION, so the worst case
        # is (pool_size + max_overflow) x cache_size_mb per pool - here (8+8 + 4+8) x 64MB =
        # 1.8GB - and this box has 10GB with NO SWAP, where running out is a livelock rather
        # than a slowdown. SQLite fills a cache lazily, so only connections that actually scan
        # approach it, but RAISING EITHER CEILING MEANS REDOING THAT ARITHMETIC FIRST.
        'pool_size': 8,          # UI connections kept warm (a warm page cache is the win in 363)
        'max_overflow': 8,       # UI burst on top, closed after use; ceiling 16
        # 16 because a browser opens ~6 connections per host per tab, so 2-3 tabs is the real
        # worst case, and since dev/changelog/418 no request can camp on a connection - a
        # search is capped at search.timeout_seconds. Deliberately only one above the 15 that
        # used to apply by accident: the fix here is the split, not a bigger number.
        'pool_timeout': 30,      # seconds a UI request waits for a connection before failing
        # 30 is longer than the longest a request may hold one (20s), so ordinary turnover
        # always wins the wait. A UI request still waiting after 30s is facing something
        # pathological and should fail loudly rather than pile up.

        'background_pool_size': 4,
        'background_max_overflow': 8,   # ceiling 12
        # 12 covers APScheduler's 10 worker threads plus the recorder watchdogs, the
        # concatenator and the post-processor, which is every background consumer there is.
        'background_pool_timeout': 60,  # a sync would rather be late than fail
    },
    'logging': {
        'level': 'INFO',
        'file': None,
        'max_bytes': 10485760,   # rotate the log file at 10 MB; <= 0 disables rotation
        'backup_count': 5,       # keep this many rotated files (~60 MB total with the default size)
    },
    'search': {
        # Wall-clock budget for one /api/channels/search request, enforced inside SQLite so
        # it can stop a statement already running (app/db_utils.py::query_deadline). Blowing
        # it is a 503, never a silently short result. Either value at 0 disables its half.
        #
        # TWO BUDGETS, because a healthy slow search and a degraded one are different
        # problems and one number cannot serve both (dev/changelog/418):
        #
        # * a request whose index is USABLE gets the backstop. It has to clear the slowest
        #   legitimate request there is. Two defects set this number in turn: the airing
        #   grain's first-paint facet request at 32-64s (90s, fixed in dev/changelog/420),
        #   then the rail's Today chip at 40s (60s, fixed in dev/changelog/421). With both
        #   gone the slowest legitimate request measured over HTTP on the live database is
        #   **9.3s** - an unbounded `custom:` window, i.e. the widest search the page can
        #   express - with Today at 6.7s and the unfiltered rail at 6.6s. 20s is ~2x that
        #   ceiling. Re-measure before moving it again; the number is a measurement, not a
        #   preference.
        # * a request running UNINDEXED gets the tight one. That is the sync-window state
        #   the incident happened in - a LIKE scan over 1.9M epg_entries rows, ten of them
        #   at once - where the request has no business grinding on and shedding it early is
        #   the point. 15s is ~13x the 1.2s that scan costs on an idle box.
        'timeout_seconds': 20,
        'degraded_timeout_seconds': 15,
        # How many searches may run WITHOUT their index at once. The other half of the same
        # incident: one unindexed scan is survivable, ten together are what saturated the
        # box, exhausted the pool, killed the sync and starved the rebuild that would have
        # ended the degraded window (dev/changelog/422). Waiting for a slot is charged
        # against the budget above, not added to it, and a request that never gets one is a
        # 503 that says so. 0 disables the cap.
        #
        # 1, on a two-core box, and the reasoning is that the scan is CPU-bound and
        # single-threaded: running two at once does not raise throughput, it just makes both
        # take twice as long, so serializing costs the second requester nothing it was not
        # already going to pay - while it keeps a core free for the rebuild and the sync that
        # END the degradation. Measured before the cap: eight concurrent degraded airing
        # searches ALL failed at the 15s budget having returned nothing, against 10.6s for
        # the same search alone.
        'max_concurrent_unindexed': 1,
        # Memoize each tag's "channels carrying it" id set (app/channel_search.py -
        # _cached_tag_channel_ids), instead of recomputing it live on every search/facet
        # count. A common-word tag pattern (`live`, `new`) can otherwise make the query
        # planner pick it as the driving predicate over far more selective filters and blow
        # past the timeout above (dev/changelog/597). Costs kilobytes to low tens of MB in
        # practice (bounded by channel count, not EPG row count - measured in the same
        # changelog); on by default. Exposed in Settings so a RAM-constrained install can
        # turn it off, which also frees whatever is currently cached.
        'tag_id_cache_enabled': True,
        # How long the airing grain's UNFILTERED standing-breakdown result (the total + the
        # four default "Hide X" counts, app/channel_search.py::_cached_standing_breakdown) may
        # be served from cache before recomputing, on top of its watermark invalidation
        # (a channel/EPG sync landing). Load-bearing, not a nicety: the watermark cannot see a
        # health-check score update, a channel-group membership edit, or wall-clock time
        # passing (the "hide past airings" toggle) - all three feed the default-on toggles, and
        # this bounds how stale the cached numbers can get from any of them
        # (dev/changelog/598). 300s (5 minutes), deliberately chosen. Exposed in Settings.
        'standing_breakdown_cache_ttl_seconds': 300,
        # Rows per page the channel search PAGE opens with when its URL names none. One of
        # channel_search.PAGE_SIZE_OPTIONS. Never read by the engine: a request with no
        # `per_page` still means DEFAULT_PAGE_SIZE, so a stored link keeps meaning what it
        # meant (dev/changelog/1043).
        'page_size': 100,
        # Budget for an OPTIONAL aggregate - the totals and the facet rail - while search is
        # running unindexed. Rows are never optional and never use this; they keep
        # `degraded_timeout_seconds` above.
        #
        # A degraded aggregate is attempted, not refused outright, because "degraded" covers
        # a fresh install whose index has never been built - where these counts cost
        # microseconds over a few hundred channels. A blanket refusal would leave that
        # install with no totals forever. So the cost decides, and this is the cut-point:
        # anything that cannot answer within it is declined and labeled as declined.
        #
        # 2s, measured. On this database one degraded aggregate is 3.6-3.8s (the LIKE COUNT
        # over 1.48M epg_entries rows, and the grouped facet scan) against ~3.4s for the row
        # page that the user is actually waiting on - so they are declined here, and served
        # on any install where they are genuinely cheap. 0 = never attempt.
        'degraded_aggregate_timeout_seconds': 2,
        # The same lever for the other reason an optional aggregate gets expensive: the index
        # is perfectly healthy, but it cannot answer the question that was asked, so counting
        # means reading the whole table (app/routes/channel_search.py::full_scan_reason - today that
        # is the airing grain with "Show airings that have ended" ticked).
        #
        # Separate from the key above because the two states are not the same fact. A degraded
        # window repairs itself in minutes and waiting is the remedy, so 2s is generous. This
        # one never repairs - it lasts exactly as long as the user leaves the option off - so
        # the budget is set by how long an optional number is worth waiting for rather than by
        # how soon it will be cheap again.
        #
        # 8s, measured. There is no cost cliff to aim at: with the option off, the facet rail
        # measures 1.2s / 1.3s / 3.5s / 4.8s / 7.6s / 8.0s / 9.2s / 17.3s / 19.4s as filters
        # narrow it, then 24.4s unfiltered - a continuous spread, so any cut point is a choice
        # about waiting, not a classifier. 8s is what the ordinary DEFAULT airing rail already
        # costs on this database (7.6s), i.e. the slowest wait this page already asks for;
        # past that the number is declined rather than waited on. 0 = never attempt.
        'full_scan_aggregate_timeout_seconds': 8,
        # How long a search index may sit unusable with nobody rebuilding it before the
        # index janitor rebuilds it itself (app/search_index.py::run_index_janitor, run every
        # 10 minutes by scheduler.py::schedule_index_janitor). 0 = never, which puts index
        # repair back on the next account sync alone.
        #
        # The grace is not a throttle, it is a right-of-way rule: the janitor is the LAST
        # resort, and a sync's own close-out rebuild is the first. A sync already in flight
        # refuses the janitor through admission, but one about to start does not - and at
        # startup APScheduler fires every missed sync interval immediately, so the sync that
        # will fix this is often seconds away. 15 minutes leaves that window clear while
        # still capping the degraded window at 15-25 minutes, against the 6.5 hours measured
        # on 2026-08-15 when a restart mid-sync left the repair to nobody (dev/changelog/680).
        'index_janitor_grace_minutes': 15,
    },
    'accounts': {
        # Fallback per-account "max simultaneous connections" cap when an account's own
        # Account.max_connections override is unset. Shared by recordings + channel tests
        # (see app/connection_limits.py); does not apply to account sync.
        'default_max_connections': 1,
    },
    'sync': {
        'sync_interval_hours': 12,
        # Days of future EPG to import AND the width of the TV Guide grid, so the guide can
        # never span more time than it has data for (DESIGN.md 12.1 - the day count in every
        # visible string is generated from this, never typed).
        'epg_days_ahead': 3,
        'epg_keep_days': 1,
        'sync_log_keep_days': 30,   # delete AccountSyncLog rows older than this; 0 = keep forever
        'request_timeout_seconds': 30,
        # Stream-URL normalization mode, the global default every account inherits unless it
        # overrides (changelog/258 Spec §1): 'disabled' | 'mpegts' | 'mpegts_live' |
        # 'hls'. Ships disabled - never rewrite a user's stream URLs unless they ask.
        # Legacy booleans from a pre-dropdown config are still accepted (True -> 'mpegts').
        'url_normalization': 'disabled',
        'epg_case_sensitive_matching': False,  # False = merge case-variant EPG IDs when matching XMLTV
        'skip_sync_if_recording_active': True,
        'skip_sync_if_recording_within_minutes': 5,
        'tester_defer_retry_minutes': 20,  # defer sync past an active test run, retry after this; 0 = skip with no retry
        'url_drift_alert_min_channels': 50,  # WARN when a sync rewrites this many channels' stream URLs; 0 = disabled
        # Refuse an EPG import when the projected entry count is below this percent of the
        # prior sync's cached epg_entry_count (DESIGN-sync-resilience.md §4) - guards against
        # a transient garbage/empty XMLTV fetch wiping future EPG. 0 = disabled.
        'epg_collapse_threshold_percent': 20,
        # Refuse to apply the provider's live-channel catalog when doing so would cut the
        # account below this percent of its prior channel_count (DESIGN-live-vod.md §4.1) -
        # a truncated catalog must not silently decimate a working channel list. The
        # playlist is then imported unfiltered and an alert is raised. 0 = disabled.
        'live_classify_collapse_threshold_percent': 20,
        # Channel lifecycle tracking (DESIGN-sync-resilience.md §5) - display thresholds
        # only, sync never deletes/disables a channel based on these. 0 = feature off.
        'channel_missing_after_days': 7,
        'channel_new_within_days': 3,
        # WARN when >= this percent of the account's prior channel_count baseline went
        # unseen in one sync (advisory only - channels are never deleted). 0 = disabled.
        'feed_shrink_percent': 50,
    },
    'display': {
        'timezone': 'America/New_York',  # IANA timezone identifier (DST-aware)
        'time_format': '12h',            # '12h' (AM/PM) or '24h'
        'nav_poll_interval_seconds': 15, # how often the nav bar (stats/alerts/activity) polls
        'guide_collapse_gaps': True,     # TV Guide: collapse non-matching time gaps when a search filter is active
    },
    'debug': {
        'xtream_debug_mode': False,  # shows Fetch & Dump / Sync from Dump buttons in UI
        'xtream_dump_dir': None,     # None → DEFAULT_XTREAM_DUMP_DIR (instance/xtream-dumps)
    },
    'http': {
        'user_agent': 'VLC/3.0.18 LibVLC/3.0.18',
    },
    # Live channel preview (app/preview.py): ffmpeg stream-copies the channel into a rolling
    # HLS window the browser plays. One preview at a time, app-wide. Not on the Settings
    # page - none of these is a value a user forms an opinion about (dev/changelog/1018).
    'preview': {
        'segment_seconds': 2,           # HLS segment length; the source's keyframe interval is the floor
        'idle_timeout_seconds': 15,     # stop when no player has fetched the playlist for this long
        'max_seconds': 600,             # hard cap - a preview is a look, not a viewer
        'connect_timeout_seconds': 20,  # stop if ffmpeg has produced no segment by then
        'dir': '',                      # '' -> system temp dir; where the rolling segment window is written
    },
    'channel_testing': {
        # Enable/schedule for the automatic guide run live on the 'TV Guide Channels'
        # system health-check row (OnDemandTestJob.is_system), not in config.
        'skip_if_recording_active': True,       # skip the TV Guide Channels run if a recording is IN_PROGRESS
        'skip_if_recording_within_minutes': 10, # skip ANY run (system or custom) if a recording starts this soon; 0 = off
        'test_duration_seconds': 30,
        'wait_between_channels_seconds': 30,
        'screenshots_enabled': True,
        'screenshots_keep_count': 5,            # per channel, oldest pruned
        'capture_scratch_dir': '',              # '' -> system temp dir; set to redirect large capture clips elsewhere
        'test_history_keep': 0,                 # 0=keep forever, N=keep last N tests per channel
        'connect_retries': 2,                   # extra connection attempts after first failure
        'connect_timeout_seconds': 15,          # seconds to wait for first byte per attempt
        'connect_retry_delay_seconds': 10,      # seconds to wait between retry attempts
        'bitrate_fail_720p_kbps': 1000,         # fail if height ≤720 and bitrate ≤ this (kbps)
        'bitrate_fail_1080p_kbps': 2000,        # fail if 720 < height ≤1080 and bitrate ≤ this (kbps)
        'bitrate_fail_4k_kbps': 3000,           # fail if height >1080 and bitrate ≤ this (kbps)
        # After the capture closes, ffprobe the stream URL once and fail the channel when it
        # declares a finite container duration - a live stream has no end, so a duration at
        # all means the provider answered with a fixed clip rather than the channel. Costs
        # one short extra probe per connected test (1.6-2.8s measured) and names a failure
        # the bitrate rule below only ever caught by side effect.
        'placeholder_source_check': True,
        # Lifetime channel health score (app/health_score.py) - undertuned defaults,
        # expect to retune once more real test/recording data accumulates.
        'health_score_half_life_samples': 5,        # score decay half-life, in observations (not days)
        # Duration that gets observation weight 1.0 (sqrt scaling) - tracks
        # test_duration_seconds above, so one default-length health check is one full-weight
        # data point. This is a SPEED knob, not a balance one: it divides every observation's
        # duration alike, so it cancels out of any test-vs-recording comparison (a 1-hour
        # recording is worth ~11 default checks at any reference) and only sets how far the
        # whole score moves per observation. Halving it is arithmetically the same as halving
        # health_score_half_life_samples. Retuned 2 -> 0.5 in config_version 7 after the test
        # default went 120s -> 30s and left every check at half weight (dev/changelog/1016).
        'reference_minutes': 0.5,
        'instability_penalty_per_hr': 2.0,           # per restart/drop-per-hour, on top of proportional time-lost
        'health_score_test_fail_floor': 10,         # quality score for a FAILED test
        'health_score_warn_penalty': 10,            # any WARN on an otherwise-COMPLETED test
        # Where the four health bands start (app/health_bands.py). Poor is always 0 - a band
        # scale needs a bottom nothing can fall through. Must descend: great > good > fair.
        'health_bands': {
            'great': 90,
            'good': 80,
            'fair': 50,
        },
        # Which band counts as failing for scheduled-recording warnings
        # (app/health_score.py::channel_failing_reason): the named band or any band below it.
        # 'none' = only hard test failures and streaks count. Replaced the numeric
        # failing_score_threshold in config_version 2 (dev/changelog/771).
        'failing_band': 'poor',
        # N-or-more consecutive FAILED tests (CANCELLED skipped) = failing, independent of
        # the score above - a channel with a good prior history can sit well above
        # failing_score_threshold through weeks of hard failures (dev/changelog/478).
        # Feeds Channel.consecutive_test_failures via channel_groups.is_streaking /
        # rank_members (last-resort failover ranking) and channel_failing_reason
        # (schedule-time + reactive warnings). 0 disables streak-based failing.
        'failing_streak_threshold': 3,
        'recording_score': {
            'fail_floor': 5,                          # quality score for a channel-attributable FAILED recording
            'near_empty_bitrate_ratio': 0.20,         # segment flagged near-empty/slate below this fraction of the recording's average bitrate
            'near_empty_min_span_seconds': 30,        # segments shorter than this are never flagged (avoid noise)
            'damage_penalty_per_pct_missing': 3,      # capture-quality correction: per % of window timeline-damaged
            'near_empty_penalty_per_pct': 2,          # capture-quality correction: per % of window near-empty/slate
            'capture_quality_source_weight': 1.0,     # fixed weight for the capture-quality correction bolt-on (not duration-scaled)
        },
        # Pre-recording health check (DESIGN-prerecord-checks.md §3): optionally test a
        # recording's channel a lead time before it starts. Off by default - it spends a
        # provider connection. RecordingProfile.pre_check_enabled overrides 'enabled' per
        # profile (None = inherit this).
        'pre_check': {
            'enabled': False,
            'lead_minutes': 15,           # fire this long before recording start
            'retry_minutes': 5,           # tester-busy retry cadence (0 = skip instead of retry)
            'min_margin_seconds': 60,     # extra slack required beyond worst-case test duration
        },
        # Maintenance window for recurring health checks marked recur_use_window=True
        # (app/check_window.py): the dispatcher runs them back-to-back inside this
        # display-timezone clock range instead of at an exact time, so they can never
        # collide. No 'enabled' flag - a window with no checks assigned costs nothing.
        'window': {
            'start': '02:00',                 # display timezone, HH:MM
            'end':   '06:00',                 # <= start means the window crosses midnight
            'dispatch_interval_minutes': 5,   # how often the dispatcher looks for the next due check
        },
    },
    'config_backup': {
        'enabled': True,
        'backup_hour_et': 1,   # Hour in configured display timezone (DST-aware)
        'backup_retention_days': 14,
        'backup_dir': DEFAULT_CONFIG_BACKUP_DIR,
    },
    'alerts': {
        # Daily db-maintenance sweep deletes dismissed alerts older than this many days.
        # Only dismissed (resolved) alerts are ever removed - active/unread ones are kept
        # regardless of age. 0 = keep forever.
        'keep_days': 90,
    },
    'notifications': {
        'push_rate_limit_seconds': 60,
        'base_url': '',   # e.g. http://192.168.1.50:5000 - used to build links in push notifications
        'services': {
            'discord':        {'enabled': False, 'url': ''},
            'home_assistant': {'enabled': False, 'url': ''},
            'pushover':       {'enabled': False, 'url': ''},
            'smtp2go':        {'enabled': False, 'url': ''},
            'whatsapp':       {'enabled': False, 'url': ''},
        },
        # No row for a type in alerts.RETIRED_ALERT_TYPES: nothing raises those, so routing
        # them would be configuration that cannot do anything (dev/changelog/928).
        'routing': {
            'LOG_ERROR':   {'in_app': True, 'push_services': []},
            'LOG_CRIT':    {'in_app': True, 'push_services': []},
            'HEALTH_CHECK_WINDOW': {'in_app': True, 'push_services': []},
        },
    },
}


# ---------------------------------------------------------------------------
# Filesystem paths derived from config values. Config paths may be written relative
# (the backup-dir defaults above are), and the app's CWD is not guaranteed - systemd,
# a Docker ENTRYPOINT and a shell all start it differently - so a relative value must
# be anchored to something fixed rather than resolved by the process CWD. That anchor is
# the app root for everything except the pre-migration DB snapshots, which anchor to the
# database they belong to instead - see db_backup_dir().
# ---------------------------------------------------------------------------

def resolve_app_path(path: str) -> str:
    """Absolutize a config-supplied path against the app root. Absolute paths pass
    through unchanged, so a user-pointed directory is never rewritten."""
    if not path:
        return path
    return path if os.path.isabs(path) else os.path.join(_APP_ROOT, path)


def db_backup_dir(cfg: dict) -> str:
    """The one reader of database.backup_dir - where pre-migration DB snapshots live.

    A relative value is anchored to the directory holding database.path, NOT to the app
    root, so the snapshots belong to the database being migrated. The two are the same
    directory in a default install and in the real one (dvr.db sits at the app root), and
    a configured absolute path is never rewritten - so this moves nobody's existing
    backups. What it closes is an app pointed at some other database: with an app-root
    anchor, a scratch app or a test that overrode only database.path still deposited its
    snapshot into the running install's folder and then pruned that folder to
    migration_backups_keep, evicting the real ones. Snapshots are the whole rollback story
    (no down-migrations), so the eviction, not the clutter, is the damage
    (dev/changelog/1052).
    """
    db_cfg = cfg.get('database', {}) or {}
    configured = db_cfg.get('backup_dir') or DEFAULT_DB_BACKUP_DIR
    if os.path.isabs(configured):
        return configured
    db_path = resolve_app_path(db_cfg.get('path') or _DEFAULTS['database']['path'])
    return os.path.join(os.path.dirname(db_path), configured)


def legacy_app_root_db_backup_dir(cfg: dict) -> str:
    """Where db_backup_dir() would have pointed before the anchor moved, or '' when the
    two agree. Only a relocated database with a relative backup_dir can differ, and such
    an install's older snapshots are still sitting in the returned directory - unpruned
    and unreferenced - so the migration path names it once rather than leaving the move
    to be discovered."""
    current = db_backup_dir(cfg)
    db_cfg = cfg.get('database', {}) or {}
    legacy = resolve_app_path(db_cfg.get('backup_dir') or DEFAULT_DB_BACKUP_DIR)
    return '' if os.path.abspath(legacy) == os.path.abspath(current) else legacy


def ensure_private_dir(path: str) -> str:
    """makedirs(path) and, only when this call created it, chmod 0700.

    Backup dirs hold secrets (DESIGN-secrets.md §5), so a dir we create is private from
    birth. A dir that already exists is left alone: its permissions are the user's
    choice, and on a network mount (CIFS forces dir_mode) a chmod would fail or be
    silently ignored anyway."""
    created = not os.path.isdir(path)
    os.makedirs(path, exist_ok=True)
    if created:
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            log.warning('Could not set 0700 permissions on %s: %s', path, exc)
    return path


_restart_needed: bool = False


def set_restart_needed(value: bool = True):
    global _restart_needed
    _restart_needed = value


def is_restart_needed() -> bool:
    return _restart_needed


_MISSING = object()


def _flatten(d, prefix=''):
    """Yield (dot.path, value) for every leaf in a nested dict."""
    if isinstance(d, dict):
        for key, val in d.items():
            path = f'{prefix}.{key}' if prefix else str(key)
            yield from _flatten(val, path)
    else:
        yield (prefix, d)


_DEFAULT_LEAVES = dict(_flatten(_DEFAULTS))


def config_default(path: str):
    """The built-in default for a dotted config path, straight from _DEFAULTS.

    A fallback that restates a default as a literal drifts from it - four Settings rows
    and nine code fallbacks had (dev/changelog/1003). Raises KeyError for a path that is
    not a leaf of _DEFAULTS, so a typo fails loudly instead of rendering a blank.
    """
    return _DEFAULT_LEAVES[path]


def changed_from_default(cfg: dict) -> list:
    """Every _DEFAULTS leaf whose value in the merged `cfg` differs from its default.

    Compared the way save_config() decides a change (_diff_leaves, plain !=), so a value
    saved back equal to its default is not a change. Paths only: the Settings page's
    changed-from-default filter needs nothing else, and a value here would be a secret
    leaving through a surface mask_config() never sees (dev/changelog/1006).
    """
    return [path for path, _, new in _diff_leaves(_DEFAULTS, cfg)
            if path in _DEFAULT_LEAVES and new is not _MISSING]


def default_display(path: str) -> str:
    """The text of a Settings row's `Default:` line: the value as config.yaml spells it.

    The one reader of "what does the page say the default is" (dev/changelog/1003).
    Pure - it reads the _DEFAULTS constant and nothing else, so the field macro may call
    it once per row. A pathless row (a notice, not a setting) gets no line.
    """
    if not path:
        return ''
    value = config_default(path)
    if value is None:
        return '(none)'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if value == '' or value == []:
        return '(empty)'
    if isinstance(value, list):
        return ', '.join(str(v) for v in value)
    return str(value)


# ---------------------------------------------------------------------------
# The raw config.yaml editor's checks. One parse, shared by its Validate button and its
# Save, so the two can never disagree about the same text (dev/changelog/1044).
# ---------------------------------------------------------------------------

# Sections keyed by names ChannelBin does not fix in advance (an alert type per routing
# row), so a key missing from _DEFAULTS there is not a typo.
_OPEN_CONFIG_MAPS = frozenset({'notifications.routing'})


def _config_problem(severity, message, path=None, line=None):
    return {'severity': severity, 'path': path, 'line': line, 'message': message}


def _type_word(value):
    if isinstance(value, bool):
        return 'true/false'
    if isinstance(value, (int, float)):
        return 'a number'
    if isinstance(value, str):
        return 'text'
    if isinstance(value, list):
        return 'a list'
    if isinstance(value, dict):
        return 'a section'
    return type(value).__name__


def _types_agree(default, value):
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(default, bool) and isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float))
    return isinstance(value, type(default))


def _key_lines(node, prefix='', lines=None, dupes=None):
    """{dot.path: 1-based line} for every mapping key in a composed PyYAML node tree, plus
    the paths written more than once (PyYAML keeps the last and says nothing)."""
    if lines is None:
        lines, dupes = {}, []
    if isinstance(node, pyyaml.MappingNode):
        for key_node, value_node in node.value:
            path = f'{prefix}.{key_node.value}' if prefix else str(key_node.value)
            if path in lines:
                dupes.append((path, key_node.start_mark.line + 1))
            lines[path] = key_node.start_mark.line + 1
            _key_lines(value_node, path, lines, dupes)
    return lines, dupes


def _check_against_defaults(data, defaults, prefix, lines, problems):
    for key, value in data.items():
        path = f'{prefix}.{key}' if prefix else str(key)
        line = lines.get(path)
        if key not in defaults:
            if prefix not in _OPEN_CONFIG_MAPS:
                problems.append(_config_problem(
                    'warning', 'Not a setting ChannelBin reads - it is kept but ignored.',
                    path, line))
            continue
        default = defaults[key]
        if value is None:
            continue  # "use the default" for a section, a real choice for a leaf (_deep_merge)
        if isinstance(default, dict):
            if not isinstance(value, dict):
                problems.append(_config_problem(
                    'error', f'This is a section of settings, so it cannot be '
                    f'{_type_word(value)}. Saving it would stop every page from loading.',
                    path, line))
                continue
            _check_against_defaults(value, default, path, lines, problems)
        elif default is not None and not _types_agree(default, value):
            hint = (' Put it in quotes - YAML reads an unquoted 1:30 as a number.'
                    if isinstance(default, str) and isinstance(value, (int, float)) else '')
            problems.append(_config_problem(
                'error', f'Should be {_type_word(default)}, not {_type_word(value)}.{hint}',
                path, line))


def check_config_text(text):
    """Parse the raw editor's text the way its Save does and say what is wrong with it.

    Returns `(data, problems)`: `data` is the parsed mapping (None when it does not parse
    into one), `problems` a list of {severity, path, line, message}. An `error` means Save
    refuses the text; a `warning` is said and saved anyway.

    A parse error reports PyYAML's line, column and problem, never its source snippet: the
    snippet is the offending line verbatim, and that line may hold a secret the user typed.
    Only shape and type are judged here - checks that need another module (the test window,
    health bands) live beside the route that calls this."""
    try:
        data = pyyaml.safe_load(text)
        root = pyyaml.compose(text)
    except pyyaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        context = exc.context or ''
        if context and exc.context_mark and mark and exc.context_mark.line != mark.line:
            context += f' that starts on line {exc.context_mark.line + 1}'
        message = '; '.join(part for part in (context, exc.problem) if part)
        column = f', column {mark.column + 1}' if mark else ''
        return None, [_config_problem(
            'error', f'Not valid YAML{column}: {message or "could not be parsed"}.',
            line=mark.line + 1 if mark else None)]
    except pyyaml.YAMLError as exc:
        return None, [_config_problem('error', f'Not valid YAML ({type(exc).__name__}).')]
    if not isinstance(data, dict):
        return None, [_config_problem(
            'error', 'config.yaml must be a set of "name: value" settings at the top level.')]
    lines, dupes = _key_lines(root)
    problems = [_config_problem('warning', 'Written more than once - only the last one counts.',
                                path, line) for path, line in dupes]
    _check_against_defaults(data, _DEFAULTS, '', lines, problems)
    return data, problems


def set_nested(d: dict, path: str, value):
    """Set a value in a nested dict using a dot-separated path, creating intermediate
    dicts as needed. Shared by the settings routes and the secret round-trip below."""
    keys = path.split('.')
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


# ---------------------------------------------------------------------------
# Settings-surface secret masking (DESIGN-secrets.md §6). _is_sensitive_path is THE
# authority for "this leaf is secret"; these helpers key off it so a new secret key is
# covered the moment it is added there.
# ---------------------------------------------------------------------------

def mask_config(cfg: dict) -> dict:
    """Return a deep copy of `cfg` with every sensitive leaf that holds a non-empty value
    replaced by MASK_SENTINEL. Every settings READ surface (API JSON, the YAML editor dump,
    the notification service URL inputs) renders this, never the raw config. An empty/unset
    secret is left empty so the UI can still show it as not-set."""
    masked = copy.deepcopy(cfg)
    for path, value in _flatten(cfg):
        if _is_sensitive_path(path) and isinstance(value, str) and value:
            set_nested(masked, path, MASK_SENTINEL)
    return masked


def restore_masked_secrets(new: dict, old: dict):
    """Round-trip guard (mutates `new` in place): for every sensitive leaf whose incoming
    value equals MASK_SENTINEL, replace it with the value stored in `old` so a masked read
    saved back unchanged is a no-op and the mask string never overwrites a real secret. If
    nothing is stored for that leaf, store '' and warn (never persist the sentinel)."""
    old_flat = dict(_flatten(old))
    for path, value in list(_flatten(new)):
        if value == MASK_SENTINEL and _is_sensitive_path(path):
            stored = old_flat.get(path)
            if isinstance(stored, str) and stored and stored != MASK_SENTINEL:
                set_nested(new, path, stored)
            else:
                log.warning('Mask sentinel received for unset/unknown secret %r; storing '
                            'empty string instead of the sentinel', path)
                set_nested(new, path, '')


def redact_sensitive_diff_lines(lines):
    """Redact secret values on unified-diff lines from the config-backup diff surface. Keyed
    off the known config structure per DESIGN-secrets.md §6 (no YAML re-parse): a `secret_key:`
    line, a `password_hash:` line, or a bare `url:` line (the notifications.services.<name>.url
    leaves - `base_url:` and other `*_url:` keys do not match). The diff prefix (+/-/space) and
    key are preserved; only a non-empty value is replaced with MASK_SENTINEL."""
    out = []
    for line in lines:
        m = _DIFF_SECRET_RE.match(line)
        if m and m.group('val').strip():
            out.append(f"{m.group('pre')}{m.group('key')}: {MASK_SENTINEL}")
        else:
            out.append(line)
    return out


# Diff line = one +/-/space prefix, indentation, the bare key, ': ', then the value.
# Keyed on literal key names rather than _is_sensitive_path() (unlike every other masker
# here) because a unified diff line has no path context to check against - it is one
# indented `key: value` line with no parent keys visible. A new _SENSITIVE_EXACT_PATHS
# leaf whose bare key differs from its full path (as auth.password_hash's does not, but
# integrations.home_assistant.api_key_hash's does) needs its own alternative added here
# explicitly.
_DIFF_SECRET_RE = re.compile(
    r'^(?P<pre>[+\- ]\s*)(?P<key>secret_key|password_hash|api_key_hash|url):\s*(?P<val>.*)$')


def _diff_leaves(old: dict, new: dict) -> list:
    """Return [(path, old_value, new_value), ...] for every leaf that differs between
    two nested config dicts. old_value/new_value is _MISSING if the leaf's path only
    exists on one side (key added/removed)."""
    old_flat = dict(_flatten(old))
    new_flat = dict(_flatten(new))
    changed = []
    for path in sorted(set(old_flat) | set(new_flat)):
        ov = old_flat.get(path, _MISSING)
        nv = new_flat.get(path, _MISSING)
        if ov != nv:
            changed.append((path, ov, nv))
    return changed


def record_config_changes(old: dict, new: dict) -> list:
    """Diff two config dicts, log one line per changed leaf, and raise the restart-needed
    flag if any changed leaf is restart-required. Returns the list of (path, old, new)
    changes found (empty if nothing changed)."""
    changed = _diff_leaves(old, new)
    restart_triggered = False
    for path, ov, nv in changed:
        needs_restart = path in RESTART_REQUIRED_KEYS
        restart_triggered = restart_triggered or needs_restart
        if _is_sensitive_path(path):
            ov_disp = '<redacted>' if ov is not _MISSING else '<unset>'
            nv_disp = '<redacted>' if nv is not _MISSING else '<unset>'
        else:
            ov_disp = '<unset>' if ov is _MISSING else ov
            nv_disp = '<unset>' if nv is _MISSING else nv
        log.info('Config changed: %s: %r -> %r%s', path, ov_disp, nv_disp,
                  ' [restart required]' if needs_restart else '')
    if restart_triggered:
        set_restart_needed(True)
    return changed


def _deep_merge(base, override):
    """`override` onto `base`, recursing into nested dicts.

    A section header with nothing under it (`logo_cache:` on its own line) parses as None,
    and means "I have set nothing here" - never "this whole section is null". Storing the
    None is what a reader cannot survive: every one of them does
    `cfg.get(section, {}).get(key)`, which raises on a None that is present, and one such
    section 500'd the Settings page (dev/docs/BUGS.md 2026-09-17). The defaults stand
    instead. A leaf whose own default is not a dict keeps taking the null, because there
    `key:` with no value is a real choice - an empty logging.file means log to stdout.
    """
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        elif val is None and isinstance(result.get(key), dict):
            continue
        else:
            result[key] = val
    return result


# ---------------------------------------------------------------------------
# Config migrations - versioned transforms for renames/moves/semantic changes.
# New keys need no migration: load_config() deep-merges _DEFAULTS underneath the
# file, so additions appear automatically. Append only, never edit a shipped step.
# ---------------------------------------------------------------------------

def _cfg_m001_xtream_to_sync(cfg: dict) -> dict:
    """The pre-versioning 'xtream' section was renamed 'sync' - carry old files
    (and restored config backups from that era) forward."""
    if 'xtream' in cfg and 'sync' not in cfg:
        cfg['sync'] = cfg.pop('xtream')
    return cfg


def _cfg_m002_failing_band(cfg: dict) -> dict:
    """`channel_testing.failing_score_threshold` (a raw score) became
    `channel_testing.failing_band` (a band name) - dev/changelog/771.

    The app had two vocabularies for one judgment: badges said Poor/Fair/Good while the
    scheduled-recording warning compared against a bare number nothing else in the UI
    mentioned. The band is now the setting and the number is derived from it.

    The old threshold maps to whichever band a score just below it fell into, so an
    untouched default (25) becomes `poor`. That does move the effective cut point - 25 to
    the Poor band's ceiling of 50 - so the transform says so at WARNING rather than
    migrating silently.
    """
    from .health_bands import BAND_KEYS, DEFAULT_FLOORS, FAILING_NONE, POOR

    ct = cfg.get('channel_testing')
    if not isinstance(ct, dict) or 'failing_score_threshold' not in ct:
        return cfg
    old = ct.pop('failing_score_threshold')
    try:
        threshold = int(old)
    except (TypeError, ValueError):
        threshold = None

    if threshold is None:
        band = POOR
    elif threshold <= 0:
        band = FAILING_NONE
    else:
        floors = (ct.get('health_bands') or {}) if isinstance(ct.get('health_bands'), dict) else {}
        # The band that a score one point below the old threshold fell into.
        probe = threshold - 1
        band = POOR
        for key in BAND_KEYS[:-1]:
            floor = floors.get(key, DEFAULT_FLOORS[key])
            if isinstance(floor, int) and probe >= floor:
                band = key
                break
    ct['failing_band'] = band
    log.warning('config migration: channel_testing.failing_score_threshold (%r) is now '
                'channel_testing.failing_band: %r - a channel counts as failing at that '
                "band's ceiling rather than at the old raw score", old, band)
    return cfg


def _cfg_m003_conversion_pre_output_timeout(cfg: dict) -> dict:
    """`recording.post_process.timeout_seconds` and `reencode_timeout_seconds` (whole-job
    deadlines) are gone; `pre_output_timeout_seconds` bounds only the phase before ffmpeg
    muxes its first frame - dev/changelog/865.

    Neither old value can be carried across. They answered "how long may this whole job
    take", a question the app no longer asks: a conversion that is still advancing now runs
    to completion. Reusing one as the new key would turn a deliberately generous whole-job
    number into an absurd pre-output budget - the config this migration was written against
    carried a hand-raised timeout_seconds of 90800, i.e. a 25-hour wait for a first frame
    that a healthy job produces in seconds. So both are dropped and the new key takes its
    default, and the transform says which values it discarded rather than deleting silently.
    """
    pp = (cfg.get('recording') or {}).get('post_process')
    if not isinstance(pp, dict):
        return cfg
    dropped = {k: pp.pop(k) for k in ('timeout_seconds', 'reencode_timeout_seconds') if k in pp}
    if dropped:
        log.warning('config migration: recording.post_process %s dropped - a conversion is no '
                    'longer bounded by a whole-job deadline, only by stall_seconds once it is '
                    'producing output. pre_output_timeout_seconds now bounds the phase before '
                    'the first output frame and takes its default (%ds)',
                    ', '.join(f'{k}={v!r}' for k, v in dropped.items()),
                    _DEFAULTS['recording']['post_process']['pre_output_timeout_seconds'])
    return cfg


def _cfg_m004_concat_progress_supervision(cfg: dict) -> dict:
    """`ffmpeg.concat_timeout_seconds` (a whole-job deadline) is gone; the concat is now
    bounded by `concat_pre_output_timeout_seconds` plus `concat_stall_seconds` -
    dev/changelog/947.

    The old value cannot be carried across, for the same reason migration 3 could not carry
    the conversion's. It answered "how long may this whole join take", a question the app no
    longer asks: a join that is still writing now runs to completion. Reusing it as either
    new key would be a different rule wearing the old number - as a pre-output budget it
    would be roughly right by accident, and as a stall budget a user who had raised it to
    survive a big recording would get a stall detector that waits hours. So it is dropped,
    both new keys take their defaults, and the transform says what it discarded rather than
    deleting silently.
    """
    ff = cfg.get('ffmpeg')
    if not isinstance(ff, dict) or 'concat_timeout_seconds' not in ff:
        return cfg
    dropped = ff.pop('concat_timeout_seconds')
    log.warning('config migration: ffmpeg.concat_timeout_seconds (%r) dropped - a concat is '
                'no longer bounded by a whole-job deadline. concat_pre_output_timeout_seconds '
                '(%ds) now bounds the phase before ffmpeg writes anything, and '
                'concat_stall_seconds (%ds) is the only bound once it is writing',
                dropped,
                _DEFAULTS['ffmpeg']['concat_pre_output_timeout_seconds'],
                _DEFAULTS['ffmpeg']['concat_stall_seconds'])
    return cfg


CONTAINER_LOG_FILE = '/config/dvr.log'


def _cfg_m005_container_log_file(cfg: dict) -> dict:
    """Containers get a log file. Without one the Logs page is permanently empty, because it
    tails `logging.file` and has no second source - dev/changelog/981.

    Containers only. On any other install "no log file" is a legitimate choice (log to
    stdout, let systemd or a supervisor keep it), so nothing is written there. Inside a
    container it is not really a choice at all: the seeded config never set it, so the page
    could not work on any container ever created, and the user never made the decision this
    would be overriding.

    Set-once, which is the whole reason this is a migration rather than a computed default.
    A user who afterwards decides they want stdout alone clears the field and it stays
    cleared - the migration has already run and will not run again. A default resolved at
    read time could not tell "never set" from "deliberately cleared" and would keep
    reimposing itself.

    `/config` rather than anywhere else because it is a declared volume: a log on the image's
    own layer is discarded at the next upgrade. Rotation is bounded by the existing
    logging.max_bytes / backup_count defaults, so this cannot grow without limit.
    """
    if not os.environ.get('CHANNELBIN_DOCKER'):
        return cfg
    logging_cfg = cfg.get('logging')
    if not isinstance(logging_cfg, dict):
        logging_cfg = {}
        cfg['logging'] = logging_cfg
    if logging_cfg.get('file'):
        return cfg
    logging_cfg['file'] = CONTAINER_LOG_FILE
    log.warning('config migration: logging.file set to %s - this container had none, so the '
                'Logs page had nothing to read. Logs still go to stdout as well, so '
                '`docker logs` is unchanged. Clear the setting if you want stdout only',
                CONTAINER_LOG_FILE)
    return cfg


def _pop_config_path(cfg: dict, path: tuple):
    """Remove the leaf at `path` from a config structure. Returns (present, value).

    Two things a plain `parent.pop(key)` does not do on the round-trip structure, both of
    which a config migration needs. The removed key's own comment goes with it, and a
    parent map left empty by the removal goes too - a comment with no key under it is
    dumped at the wrong indentation and a map emptied this way is dumped as `{}` in the
    middle of a block, and ruamel cannot read back either (dev/docs/BUGS.md 2026-09-17).
    """
    parents = []
    node = cfg
    for key in path[:-1]:
        if not isinstance(node, dict) or key not in node:
            return False, None
        parents.append((node, key))
        node = node[key]
    if not isinstance(node, dict) or path[-1] not in node:
        return False, None
    value = node.pop(path[-1])
    _forget_comment(node, path[-1])
    for parent, key in reversed(parents):
        if parent[key]:
            break
        del parent[key]
        _forget_comment(parent, key)
    return True, value


def _forget_comment(parent, key):
    """Drop the round-trip comment attached to `key`, which no longer exists. A no-op on a
    plain dict, which carries no comments at all."""
    items = getattr(parent, 'ca', None)
    if items is not None:
        items.items.pop(key, None)


# The three per-kind image folder keys recording.images_dir replaced, with the defaults they
# shipped with (the container's seed config wrote the logo one out as a value, so it counts).
_LEGACY_IMAGE_DIR_KEYS = (
    (('channel_testing', 'screenshot_dir'), ('/dvr/channel_test_screenshots',)),
    (('recording', 'live_thumbnail', 'dir'), ('/dvr/live_thumbnails',)),
    (('recording', 'logo_cache', 'dir'), ('instance/logo-cache', '/config/instance/logo-cache')),
)


def _cfg_m006_one_images_dir(cfg: dict) -> dict:
    """Thumbnails, screenshots and cached logos share one `recording.images_dir`, each in
    its own subfolder - dev/changelog/1012.

    A folder the user actually chose carries over as images_dir: the screenshot folder
    first, because it was the only one of the three Settings ever showed, then the thumbnail
    folder, then the logo folder. A value equal to its old default was never a choice and
    carries nothing. Files already saved are not moved, and the warning names the folders
    they are still in.
    """
    found = []
    for path, defaults in _LEGACY_IMAGE_DIR_KEYS:
        present, value = _pop_config_path(cfg, path)
        if not present:
            continue
        found.append(('.'.join(path), value, bool(value) and value not in defaults))
    if not found:
        return cfg
    chosen = next(((key, value) for key, value, is_choice in found if is_choice), None)
    rec = cfg.get('recording')
    if not isinstance(rec, dict):
        rec = {}
        cfg['recording'] = rec
    if chosen and not rec.get('images_dir'):
        rec['images_dir'] = chosen[1]
    images_dir = rec.get('images_dir') or _DEFAULTS['recording']['images_dir']  # pre-merge dict
    log.warning('config migration: %s replaced by recording.images_dir (%s)%s. New images go '
                'to its thumbnails, screenshots and logos subfolders; files already saved '
                'were not moved and stay in %s',
                ', '.join(key for key, _, _ in found), images_dir,
                f', carried over from {chosen[0]}' if chosen else '',
                ', '.join(str(value) for _, value, _ in found if value))
    return cfg


def _cfg_m007_reference_minutes_follows_test_duration(cfg: dict) -> dict:
    """`channel_testing.reference_minutes` 2 -> 0.5, so the full-weight observation length
    is one default-length health check again - dev/changelog/1016.

    The reference was set when a health check ran 120s. The test default became 30s, which
    left every default check carrying sqrt(0.5/2) = half weight and halved the rate at which
    the score responds to anything. It is a speed knob rather than a balance one - it divides
    every observation's duration alike, so recordings speed up by the same factor and their
    weight *relative* to a check is unchanged.

    A stored 2 is the old default and was never a choice, so it is dropped and the new default
    applies; any other stored value was deliberate and is left alone. Scores are not recomputed
    and cannot be: app/health_recompute.py replays the weight each blend actually ran with, so
    past observations keep theirs and channels drift onto the new scale as new checks land.
    """
    ct = cfg.get('channel_testing')
    if not isinstance(ct, dict) or ct.get('reference_minutes') != 2:
        return cfg
    ct.pop('reference_minutes')
    log.warning('config migration: channel_testing.reference_minutes dropped (was the old '
                'default, 2) and now takes its new default of %s - one %ss health check is a '
                'full-weight observation again. Every channel health score will move about '
                'twice as fast per observation from here on; stored scores are unchanged',
                _DEFAULTS['channel_testing']['reference_minutes'],
                _DEFAULTS['channel_testing']['test_duration_seconds'])
    return cfg


CONFIG_MIGRATIONS = [
    (1, "rename legacy 'xtream' section to 'sync'", _cfg_m001_xtream_to_sync),
    (2, "channel_testing.failing_score_threshold -> failing_band", _cfg_m002_failing_band),
    (3, 'post_process conversion deadlines -> pre_output_timeout_seconds',
     _cfg_m003_conversion_pre_output_timeout),
    (4, 'ffmpeg.concat_timeout_seconds -> concat progress supervision',
     _cfg_m004_concat_progress_supervision),
    (5, 'containers with no logging.file get one, so the Logs page has a source',
     _cfg_m005_container_log_file),
    (6, 'thumbnail, screenshot and logo folders -> one recording.images_dir',
     _cfg_m006_one_images_dir),
    (7, 'channel_testing.reference_minutes follows the 30s test duration',
     _cfg_m007_reference_minutes_follows_test_duration),
]

CURRENT_CONFIG_VERSION = CONFIG_MIGRATIONS[-1][0]
# Keep the default stamp in lockstep with the migration list (defined above it in the file).
_DEFAULTS['config_version'] = CURRENT_CONFIG_VERSION


def migrate_config(config_overrides=None):
    """Bring config.yaml to CURRENT_CONFIG_VERSION. Runs once at startup (create_app),
    NOT inside load_config() - load_config is called constantly at runtime. The file is
    backed up via the existing config-backup subsystem before any transform touches it.

    config_overrides: the same dict create_app() was given, so the pre-migration backup
    honors a test's redirected config_backup.backup_dir instead of falling back to the
    real instance/config-backups/ - a bare do_backup() call ignored it and copied the
    developer's real config.yaml into the real backup dir on every test-app build
    (dev/changelog/620).

    Reads through _load_config_file() (the mtime cache), never a direct yaml.safe_load: a
    second uncached parse here cost every create_app() ~13ms, which the test suite pays 751
    times over. The deep copy _load_config_file() returns is what makes mutating `raw` and
    writing it back below safe, and the write invalidates the cache by mtime.
    round_trip=True so the rewrite below preserves any hand-written comments.

    Holds config_write_lock across the whole read-transform-write, same as save_config():
    a save landing between this read and this write would be overwritten by the migrated
    structure built from the pre-save file."""
    with config_write_lock:
        raw = _load_config_file(round_trip=True)
        if raw is None:
            return  # fresh install - first save_config() writes the current stamp from _DEFAULTS
        file_version = raw.get('config_version', 0)
        if file_version > CURRENT_CONFIG_VERSION:
            # Unlike the DB (hard refusal), config is forgiving: defaults deep-merge under it
            # and unknown keys are ignored, so warn and continue rather than refuse to start.
            log.warning('config.yaml is config_version %d but this build only knows %d - '
                        'running anyway; some settings may be ignored',
                        file_version, CURRENT_CONFIG_VERSION)
            return
        if file_version == CURRENT_CONFIG_VERSION:
            return
        from .config_backup import do_backup, get_backup_dir
        do_backup(backup_dir=get_backup_dir(load_config(overrides=config_overrides)))
        for version, description, fn in CONFIG_MIGRATIONS:
            if version <= file_version:
                continue
            raw = fn(raw)
            log.info('Applied config migration %d: %s', version, description)
        raw['config_version'] = CURRENT_CONFIG_VERSION
        # `raw` is the round-trip structure _load_config_file() returned (comment-carrying,
        # mutated in place above) - must go through _write_config_file()'s ruamel dump, not
        # plain yaml.dump, which can't represent a CommentedMap at all.
        _write_config_file(raw)
        log.info('config.yaml migrated to config_version %d', CURRENT_CONFIG_VERSION)


# Parsed-config.yaml cache, keyed on (st_mtime_ns, st_size). load_config() is reachable
# from template filters inside per-row loops (the local_time* filters call it 4x per row
# on /), and an uncached yaml.safe_load costs ~13ms on this box - the cache turns each
# call into a stat(). Swapped as one tuple so concurrent threads need no lock: the worst
# race is a redundant re-parse, never a torn read.
_yaml_cache = None  # (stat_key, parsed_dict) or None


def _parse_config_file():
    """The actual disk read + YAML parse - kept as its own seam so the scaling
    regression test can count real parses (cache hits must not reach this).

    Round-trip parse (not yaml.safe_load): the result is a comment-carrying CommentedMap,
    not a plain dict. This is what lets save_config()/migrate_config() merge new values
    into the existing structure in place so untouched keys keep their comments, instead of
    the whole file being re-serialized from scratch. _load_config_file() converts back to
    plain dict/list/int/float/str/bool for every caller except those three - see _to_plain()
    below for why that conversion has to happen.

    Holds config_write_lock for the parse itself - not to protect the file (os.replace()
    below makes every write atomic), but because _yaml_rt is one shared instance and a
    dump running concurrently on it can corrupt this load. The mtime cache means this runs
    on a miss, not per call, so the lock is not on any hot path."""
    with config_write_lock:
        with open(_CONFIG_PATH, 'r') as f:
            return _yaml_rt.load(f) or {}


def _fsync_dir(directory):
    """Best-effort fsync of a directory, so a rename into it survives a power loss.
    Not fatal if it fails - the rename itself is already atomic against a process crash,
    which is the failure this app can actually do something about."""
    try:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        log.warning('Could not fsync %s after writing config.yaml: %s', directory, exc)


def _write_target(path):
    """The real file a config write must land on, with every symlink in `path` resolved.

    Both writers below resolve before doing anything else, because the unresolved path
    breaks all three of the properties they exist to provide. config.yaml is a symlink
    wherever the app's own directory is read-only or root-owned but its data has to
    persist elsewhere - the Docker image is the shipped instance of that, symlinking
    /app/config.yaml onto the /config volume (dev/changelog/517).

    Against that layout, writing through the symlink path means: the temp file is
    attempted in the app root, which the unprivileged runtime user cannot write; that
    directory is a different filesystem from the real target, so os.replace() would raise
    EXDEV even with permission; and had the rename somehow succeeded it would have
    replaced the SYMLINK with a regular file, silently ending config persistence at the
    next image upgrade. Three failures out of one dirname() - dev/changelog/909."""
    return os.path.realpath(path)


def _config_tmp_file(dest):
    """(fd, path) for a new temp file in `dest`'s OWN directory.

    Same directory, deliberately, not the system temp dir: os.replace() is only atomic
    within a single filesystem, and config.yaml can sit anywhere (the app root in
    production, a per-test temp dir under ConfigSandbox, another filesystem entirely when
    the app root's copy is a symlink). Callers pass a _write_target()-resolved `dest` for
    that last reason - "the directory the path names" and "the directory the file is in"
    are not the same place through a symlink. A cross-filesystem replace would silently
    degrade back into the copy-then-truncate hazard this exists to remove.

    mkstemp creates it 0600, so a config.yaml written for the first time is private from
    birth rather than umask-derived - it carries flask.secret_key and the auth hash."""
    return tempfile.mkstemp(prefix='.config.yaml.', suffix='.tmp',
                            dir=os.path.dirname(dest) or '.')


def _finish_replace(tmp_path, dest):
    """Carry `dest`'s existing permissions onto tmp_path, then os.replace() it over dest.
    os.replace() moves the temp file's own mode with it, so without this a 0600
    config.yaml would silently widen or narrow on every save."""
    try:
        os.chmod(tmp_path, os.stat(dest).st_mode & 0o7777)
    except OSError:
        pass  # no file yet (first write) - keep mkstemp's private 0600
    os.replace(tmp_path, dest)
    _fsync_dir(os.path.dirname(dest) or '.')


def _serialize_config(data) -> str:
    """The exact text that will become config.yaml, proven to parse back before it is
    installed.

    A round-trip structure that has had keys removed can carry a comment with no key left
    to hold it, and ruamel then dumps YAML it cannot itself read - which is a config.yaml
    that loads on nobody's machine and an app that cannot start at all (dev/docs/BUGS.md
    2026-09-17, config migration 6 on a container's commented seed file). The values are
    what must survive, so an unreadable round-trip dump falls back to a plain one, losing
    the file's comments and saying so in the log, rather than installing a file no reader
    can load. A plain dump that still does not parse is not recoverable here and raises,
    which leaves the existing config.yaml untouched - _write_config_file() only replaces
    the file once this has returned."""
    buf = io.StringIO()
    _yaml_rt.dump(data, buf)
    text = buf.getvalue()
    try:
        _yaml_rt.load(text)
        return text
    except YAMLError as exc:
        log.warning('config.yaml could not be written with its comments intact (%s) - '
                    'writing it without them so the file stays readable. Every setting is '
                    'preserved; hand-written comments in it are lost', exc)
    buf = io.StringIO()
    _yaml_rt.dump(_to_plain(data), buf)
    text = buf.getvalue()
    _yaml_rt.load(text)
    return text


def _write_config_file(data):
    """THE writer of config.yaml - every path that rewrites the file goes through here or
    _replace_config_file_from() below (enforced by
    tests/test_static_invariants.py::ConfigWriteBypassTests), the write-side counterpart to
    _parse_config_file() being its one reader.

    Dumps to a temp file, fsyncs it, and os.replace()s it into place, so the file is never
    observable half-written. What this replaced - open(_CONFIG_PATH, 'w') followed by a
    dump - truncated first and wrote after, so a crash, an OOM-kill or an exception raised
    mid-dump left an empty or truncated config.yaml: no secret_key, no auth hash, every
    session invalid and the user locked out. A concurrent reader could also parse the
    truncated file and get a config missing whole sections.

    `data` may be a ruamel CommentedMap (the round-trip structure, comments preserved) or
    a plain dict. Callers must hold config_write_lock across their whole read-merge-write.
    """
    text = _serialize_config(data)
    dest = _write_target(_CONFIG_PATH)
    fd, tmp_path = _config_tmp_file(dest)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _finish_replace(tmp_path, dest)
    except BaseException:
        # A failed write must leave both the temp file and the previous config.yaml
        # exactly as they were - never a half-written file under either name.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _replace_config_file_from(src, dest=None):
    """Atomically make config.yaml (or `dest`) a byte-for-byte copy of `src` (a config
    backup).

    Separate from _write_config_file() because a restore must reproduce the backup's exact
    bytes - re-serializing a parsed structure would silently reformat the file the user
    asked to be put back. copy2 semantics, so the backup's own mode/timestamps carry over
    exactly as they did when this was a direct copy2 onto the live path.

    Callers must hold config_write_lock."""
    if dest is None:
        dest = _CONFIG_PATH
    dest = _write_target(dest)
    fd, tmp_path = _config_tmp_file(dest)
    try:
        # copyfileobj + copystat is what copy2 does; decomposed only so the fsync lands
        # on the temp file's own write handle before anything is renamed into place.
        with os.fdopen(fd, 'wb') as f:
            with open(src, 'rb') as srcf:
                shutil.copyfileobj(srcf, f)
            f.flush()
            os.fsync(f.fileno())
        shutil.copystat(src, tmp_path)
        os.replace(tmp_path, dest)
        _fsync_dir(os.path.dirname(dest) or '.')
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _to_plain(obj):
    """Recursively strip ruamel's round-trip wrapper types (CommentedMap/CommentedSeq,
    ScalarInt/ScalarFloat, the ScalarString family) down to plain dict/list/int/float/str.

    These wrapper types are real subclasses of the builtins (isinstance checks and normal
    dict/list operations all still work), so nothing inside this module needed to change -
    but a subclass PyYAML has no representer for gets dumped by the *non-safe* yaml.Dumper
    as a generic `!!python/object/new:...` tag, which yaml.safe_load() then refuses to
    construct at all. load_config()'s result is read by code far outside this module
    (JSON API responses, the raw-YAML-editor's own yaml.dump(), plain equality/repr in
    logs) that has every reason to expect the exact plain types yaml.safe_load() always
    returned - so nothing downstream of load_config() should ever see a wrapper type."""
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return float(obj)
    if isinstance(obj, str):
        return str(obj)
    return obj


def _load_config_file(round_trip=False):
    """Parsed config.yaml dict, or None if the file doesn't exist. Callers can mutate
    their result freely (the settings save flow does) without corrupting the cache -
    preserving the semantics of a fresh parse per call.

    round_trip=True returns a deep copy of the comment-carrying round-trip structure the
    cache already holds, for a caller about to mutate and rewrite the file that wants
    existing comments preserved (save_config(), migrate_config(), the legacy
    channel_testing key strip in app/__init__.py). Every other caller - notably
    load_config() - gets plain types back via _to_plain(), which already builds brand-new
    dicts/lists at every level, so no separate deep copy is needed on that path."""
    global _yaml_cache
    try:
        st = os.stat(_CONFIG_PATH)
    except OSError:
        _yaml_cache = None
        return None
    key = (st.st_mtime_ns, st.st_size)
    cached = _yaml_cache
    if cached is None or cached[0] != key:
        cached = (key, _parse_config_file())
        _yaml_cache = cached
    if round_trip:
        return copy.deepcopy(cached[1])
    return _to_plain(cached[1])


def load_config(overrides=None):
    """Load config with defaults, then config.yaml, then optional `overrides` on top.

    `overrides` (a nested dict, deep-merged last) is the test seam: it lets a throwaway
    test app point database.path at a temp file, redirect output dirs, etc., without
    touching config.yaml. Default None → today's exact behavior (defaults + file only),
    so the production no-arg call is byte-identical.

    Must be copy.deepcopy, not .copy(): a shallow copy shares every nested dict (e.g.
    config['auth']) with _DEFAULTS itself by reference. _deep_merge() only replaces a
    nested dict when the same key exists in `override` too - a top-level section absent
    from config.yaml (true of every install until its first save, and forever true of a
    section nobody has touched yet) passes straight through as that same shared
    reference. set_nested()'s dict.setdefault() then mutates it in place, permanently
    corrupting the process-wide _DEFAULTS - the next load_config() anywhere in the
    process, for any config.yaml, inherits the poisoned value as its "default" (dev/docs/BUGS.md
    2026-08-06 - caught via auth.password_hash, the first-ever brand-new top-level
    section to hit this path in production use).
    """
    config = copy.deepcopy(_DEFAULTS)
    file_config = _load_config_file()
    if file_config is not None:
        config = _deep_merge(config, file_config)
    if overrides:
        config = _deep_merge(config, overrides)
    return config


def load_for_edit():
    """Return `(merged, file_cfg)` for a settings save that changes one or a few leaves.

    `merged` is the effective config (defaults under config.yaml) and is what a route
    validates against - a value the user has never set exists only as a default, so
    validating against the file dict alone would read it as absent. `file_cfg` is the raw
    config.yaml dict: mutate **that** one and hand it to save_config().

    Writing load_config()'s merged result back is the thing this exists to prevent - it
    bakes every untouched default into config.yaml as if the user had chosen it, so a later
    change to a shipped default never reaches that install again and a deliberately-minimal
    seed file (docker/config.docker.yaml) becomes a full dump on the first save
    (dev/changelog/727).

    Caller holds config_write_lock across both loads, its mutation, and the save - the whole
    read-modify-write is one unit (dev/docs/BUGS.md 2026-08-15 @ 05:32:07 PM ET)."""
    return load_config(), (_load_config_file() or {})


def _merge_into_yaml_map(base, new):
    """Apply `new`'s values onto `base` (a ruamel CommentedMap, or a plain dict for a
    fresh-install file that doesn't exist yet) in place, recursing into nested dicts.
    Preserves whatever comment ruamel attached to a key that survives the merge - only the
    value changes, the key node itself (and its comment) is untouched. A key present in
    `base` but absent from `new` is deleted, matching save_config()'s existing full-replace
    semantics (today's plain yaml.dump(data) already writes exactly `data` and nothing
    left over from a prior save)."""
    for key, val in new.items():
        if isinstance(val, dict) and key in base and isinstance(base[key], dict):
            _merge_into_yaml_map(base[key], val)
        else:
            base[key] = val
    for key in list(base.keys()):
        if key not in new:
            del base[key]
            _forget_comment(base, key)
    return base


def save_config(data):
    """Write config.yaml and log/flag whatever actually changed vs. the previous config.
    Returns the list of (path, old, new) changes found (empty if the save was a no-op).

    Merges `data` into the file's own existing structure (via _load_config_file(), the
    same single reader every other caller uses) rather than dumping `data` on its own -
    that's what lets untouched keys keep their hand-written comments (BUGS.md 2026-08-12)
    instead of the whole file being re-serialized from scratch.

    The lock spans the whole read-merge-write, not just the write: two concurrent saves
    that each read before either writes both merge onto the same stale base, and the
    second write drops the first's change silently (BUGS.md 2026-08-15).

    The change list is diffed EFFECTIVE against EFFECTIVE - the old merged config against
    `data` with the defaults merged underneath it - never against `data` on its own. `data`
    may legitimately be the sparse config.yaml dict (load_for_edit()'s contract), and
    diffing a sparse file dict against the merged old config reports every unset key as
    removed: on a minimal config.yaml that is ~170 phantom 'Config changed' lines and a
    false restart banner, the same defect dev/changelog/110 fixed. A key absent from `data`
    means "use the default", so the default is what its new effective value is
    (dev/changelog/727). config_backup.py::apply_backup already diffs this way."""
    with config_write_lock:
        old = load_config()
        # Round-trip guard: any sensitive leaf submitted as MASK_SENTINEL keeps its stored
        # value, so saving a masked read surface back unchanged never overwrites a real secret.
        restore_masked_secrets(data, old)
        existing = _load_config_file(round_trip=True)
        if existing is None:
            existing = CommentedMap()  # fresh install - no file, and so no comments, yet
        merged = _merge_into_yaml_map(existing, data)
        _write_config_file(merged)
        return record_config_changes(old, _deep_merge(copy.deepcopy(_DEFAULTS), data))


# Where a resolved binary came from - the half of the answer a bare path does not give.
# Defined here, beside the two resolvers that produce it, and re-exported by
# app/toolchain.py, which is what carries it to the API and the Maintenance card. There
# were two of these until dev/changelog/914 gave ffprobe a configured path of its own and
# with it a third way to be found.
SOURCE_CONFIGURED = 'configured'   # an absolute/relative path the user set in Settings
SOURCE_SIBLING = 'sibling'         # found next to a configured ffmpeg, under its directory
SOURCE_PATH = 'path'               # found on PATH under its plain name


def resolve_ffmpeg_path(configured_path: str = 'ffmpeg') -> str:
    """Return the ffmpeg binary path - the one place that answers "which ffmpeg".

    configured_path is returned whether or not it resolves, so a caller that cannot run it
    fails with an OS error naming the tool rather than on an empty argv[0]. A leading ~ is
    expanded, because nothing else in the stack will: the value reaches subprocess as
    argv[0] with no shell in between, so "~/opt/ffmpeg-7.1/bin/ffmpeg" is an ENOENT rather
    than a path, and the settings field is exactly where a user types one.

    There is deliberately no bundled fallback. Until dev/changelog/911 this reached for
    imageio-ffmpeg's static binary when nothing was on PATH, and that fallback supplied
    exactly half a toolchain: the package ships an ffmpeg and no ffprobe, so an install
    without a system ffmpeg captured happily while every probe failed and returned {} -
    no format detection, no recording health numbers, no format lock. Two further costs
    made it a net negative rather than a partial win. Screenshots do not work on that
    binary at all (dev/docs/BUGS.md 2026-09-08: the argv that writes a 4108-byte jpg under
    6.1.1 exits 0 having written nothing under its bundled 7.0.2), and a resolver that
    returns a path which exists when no real ffmpeg does defeated two test files' skip
    guards in turn (dev/docs/BUGS.md 2026-09-08). Both binaries now come from one install
    or neither does, and app/toolchain.py says which it is.
    """
    return os.path.expanduser(configured_path)


def _ffprobe_beside(configured_ffmpeg_path: str):
    """The ffprobe sitting in the same directory as a configured ffmpeg, or None.

    Only applies to a configured path that carries a directory. A bare "ffmpeg" would
    resolve through PATH to somewhere like /usr/bin, whose ffprobe is the one PATH would
    have found anyway - so claiming SOURCE_SIBLING for it would be a provenance that
    describes nothing.

    Existence is checked rather than assumed, so a configured ffmpeg with no ffprobe
    beside it falls through to PATH instead of pinning the probe to a path that cannot
    run. That fall-through is what keeps this a preference rather than a trap.
    """
    ffmpeg_path = os.path.expanduser((configured_ffmpeg_path or '').strip())
    if os.sep not in ffmpeg_path:
        return None
    candidate = os.path.join(os.path.dirname(ffmpeg_path), 'ffprobe')
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


def describe_ffprobe_resolution(configured_path=None, configured_ffmpeg_path=None):
    """(path, source) for ffprobe - the one place that answers "which ffprobe, and why".

    Three answers, in this order:

      1. `ffmpeg.ffprobe_path`, when set. Returned whether or not it resolves, matching
         resolve_ffmpeg_path's contract: a caller that cannot run it fails with an OS error
         naming the tool rather than on an empty argv[0], and app/probe.py reads exactly
         that error as the one probe failure that is a configuration problem rather than a
         fact about the file.
      2. an ffprobe beside a configured `ffmpeg.path`, which is how every real install is
         laid out - a distro's /usr/bin, a static build's bin/. This is what makes moving
         one setting move the whole toolchain, which is the ordinary case and the reason
         the key above can stay empty on almost every install.
      3. PATH, else the bare name.

    Until dev/changelog/914 only the third existed, justified on the grounds that ffprobe
    ships beside ffmpeg in every real install - true of a distro install, and false the
    moment anyone points ffmpeg.path at a side-by-side build. The result was a SPLIT
    toolchain: captures and conversions on the configured binary while every probe, every
    health number, every recorded format field and every format lock was measured by
    whatever PATH held. That is not hypothetical - it is what a 7.1.5 build at
    ~/opt/ffmpeg-7.1/ did on the machine this app is developed on, and why dev/changelog/913
    had to A/B by prepending PATH rather than by using the setting that exists for it.

    Both values are read from config when not supplied. That is a stat() against
    load_config()'s mtime cache, against the process spawn every caller is about to pay,
    and no call site is inside a per-row loop - app/probe.py's three are each one probe of
    one file. A caller that already holds a config (app/toolchain.py) passes both anyway.
    """
    if configured_path is None or configured_ffmpeg_path is None:
        ffmpeg_cfg = load_config().get('ffmpeg', {})
        if configured_path is None:
            configured_path = ffmpeg_cfg.get('ffprobe_path', '')
        if configured_ffmpeg_path is None:
            configured_ffmpeg_path = ffmpeg_cfg.get('path', 'ffmpeg')
    configured_path = (configured_path or '').strip()
    if configured_path:
        return os.path.expanduser(configured_path), SOURCE_CONFIGURED
    sibling = _ffprobe_beside(configured_ffmpeg_path)
    if sibling:
        return sibling, SOURCE_SIBLING
    return shutil.which('ffprobe') or 'ffprobe', SOURCE_PATH


def resolve_ffprobe_path(configured_path=None, configured_ffmpeg_path=None) -> str:
    """The ffprobe binary path - the sibling of resolve_ffmpeg_path above.

    The path half of describe_ffprobe_resolution(), which is where the reasoning lives.
    Two entry points rather than one with a flag, so the spawn sites in app/probe.py ask
    for what they need (a path) and app/toolchain.py asks for what it needs (a path and
    the provenance it reports), and neither re-derives the other's answer.
    """
    return describe_ffprobe_resolution(configured_path, configured_ffmpeg_path)[0]


def parse_tristate_bool(val: str):
    """Convert a form value ('true'/'false'/'') to Boolean or None - the shared
    'blank = use global default' convention for nullable Boolean override fields
    (HealthCheckProfile.screenshots_enabled). Account.url_normalization used this before
    _m014 turned it into a four-way mode; it now goes through
    accounts.coerce_normalization_mode instead."""
    if val == 'true':
        return True
    if val == 'false':
        return False
    return None
