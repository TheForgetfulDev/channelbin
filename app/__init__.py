import json
import logging
import logging.handlers
import os
import threading

from flask import Flask, current_app, flash, jsonify, redirect, request, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError

from .db_utils import BACKGROUND_BIND, WorkloadRoutedSession
from .fs_utils import PATH_MISSING, PATH_OK, describe_dir_problem, probe_dir

# session_options['class_'] is Flask-SQLAlchemy's documented seam for customizing db.session.
# WorkloadRoutedSession is what sends work with no request behind it to the background
# connection pool, so UI traffic cannot take the connection a sync or a recording needs -
# see its docstring for the rule and dev/changelog/423 for the incident behind it.
db = SQLAlchemy(session_options={'class_': WorkloadRoutedSession})
csrf = CSRFProtect()


def create_app(config_overrides=None, start_scheduler=True):
    """Build the Flask app.

    config_overrides: nested dict deep-merged on top of config.yaml (the test seam -
        temp DB path, redirected output dirs). None → production behavior unchanged.
    start_scheduler: when False, skips the live APScheduler/jobstore/resume machinery
        so tests get a plain request-serving app. None of the prod call sites pass
        either arg, so create_app() stays byte-identical in production.
    """
    app = Flask(__name__, template_folder='../templates', static_folder='../static')

    from .config import load_config, migrate_config
    from .version import __version__
    cfg = load_config(overrides=config_overrides)

    _setup_logging(cfg)
    logging.getLogger(__name__).info('ChannelBin %s starting', __version__)

    # Bring config.yaml forward across key renames/moves, then re-read it so the rest
    # of startup sees the migrated values. (The pre-migration load above only feeds
    # _setup_logging, whose keys have never been migrated.)
    migrate_config(config_overrides)
    cfg = load_config(overrides=config_overrides)

    app.secret_key = _resolve_secret_key(cfg)
    db_uri = 'sqlite:///' + cfg['database']['path']
    app.config['SQLALCHEMY_DATABASE_URI'] = db_uri
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    # Stored on the app rather than re-read later: init_scheduler() builds its own engine
    # and must configure it from the app it was handed, never a fresh load_config() - that
    # is how a test app's jobstore came to write into the live dvr.db (BUGS.md 2026-07-18).
    from .db_utils import DEFAULT_CACHE_SIZE_MB, DEFAULT_WAL_SIZE_LIMIT_MB
    app.config['SQLITE_CACHE_SIZE_MB'] = cfg['database'].get(
        'cache_size_mb', DEFAULT_CACHE_SIZE_MB)
    app.config['SQLITE_WAL_SIZE_LIMIT_MB'] = cfg['database'].get(
        'wal_size_limit_mb', DEFAULT_WAL_SIZE_LIMIT_MB)

    # Same reasoning as the two above: init_scheduler() reads this from app.config, never a
    # fresh load_config(), so config_overrides reaches it and a test app gets its own private
    # pidfile instead of racing every other test process against the real repo's instance/
    # (dev/docs/BUGS.md 2026-08-14 - the singleton guard this claims for).
    from .config import resolve_app_path, DEFAULT_PIDFILE_PATH
    app.config['PIDFILE_PATH'] = resolve_app_path(
        cfg['flask'].get('pidfile_path') or DEFAULT_PIDFILE_PATH)

    # Same reasoning as the two above, and the same hazard: _launch_segment runs in a
    # background thread, so resolving this from a runtime load_config() would read the real
    # config.yaml and spool a test's capture stderr into the production directory. Resolved
    # here, where config_overrides are honored, so make_test_app can sandbox it.
    capture_log_dir = resolve_app_path(cfg['recording'].get('capture_log_dir', 'capture-logs'))
    app.config['CAPTURE_LOG_DIR'] = capture_log_dir
    # Creating the directory is every app build's business - CAPTURE_LOG_DIR has to be
    # usable the moment one spawns a capture. Sweeping the spools inside it is NOT: that
    # moved to init_scheduler(), behind the pidfile claim, in dev/changelog/967. Sweeping
    # here deleted a live recording's spool whenever a second app was built against the
    # real config while the service was up, and the segment lost its diagnostics silently.
    try:
        os.makedirs(capture_log_dir, exist_ok=True)
    except OSError as exc:
        # Not fatal: capture must still run without its diagnostics (Product Principle 2 -
        # a diagnostic never harms the capture). _launch_segment degrades to DEVNULL.
        logging.getLogger(__name__).warning(
            'Could not create capture log dir %s: %s - segment stderr will not be captured',
            capture_log_dir, exc)

    # Same pattern as SQLITE_CACHE_SIZE_MB / CAPTURE_LOG_DIR above: resolved from the
    # already-loaded `cfg` (which honors config_overrides), never via a fresh
    # app.auth.refresh_auth() call here - that re-reads config.yaml with no overrides, so
    # calling it at create_app() would silently replace a test's extra_overrides={'auth':
    # ...} with whatever's on disk. refresh_auth() is for the *runtime* re-read the
    # settings write paths need after a password/settings change.
    from datetime import timedelta
    auth_cfg = dict(cfg.get('auth') or {})
    app.config['AUTH'] = auth_cfg
    timeout_minutes = auth_cfg.get('session_timeout_minutes') or 0
    # 0 = stay logged in indefinitely on that device, which still needs a real cookie
    # expiry to survive a browser restart - a year is effectively "forever" here.
    app.config['PERMANENT_SESSION_LIFETIME'] = (
        timedelta(minutes=timeout_minutes) if timeout_minutes > 0 else timedelta(days=365))

    # Two pools on one database file. Both sizes are chosen, not inherited: before
    # dev/changelog/423 there was no pool setting anywhere in the tree, so SQLAlchemy's 5 + 10
    # default applied by accident and UI traffic and background work competed for the same 15
    # connections - which is how a burst of search scans came to kill an account sync. Every
    # number, and the memory arithmetic that bounds them, is reasoned out in app/config.py's
    # `database` defaults; changing them there is the supported way, and takes effect on
    # restart because engines are built once, here.
    #
    # MUST be set before db.init_app(app) below - init_app builds the engines and, in its own
    # words, "changes to application config after this call will not be reflected".
    db_cfg = cfg['database']
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_size': db_cfg['pool_size'],
        'max_overflow': db_cfg['max_overflow'],
        'pool_timeout': db_cfg['pool_timeout'],
    }
    app.config['SQLALCHEMY_BINDS'] = {
        BACKGROUND_BIND: {
            'url': db_uri,
            'pool_size': db_cfg['background_pool_size'],
            'max_overflow': db_cfg['background_max_overflow'],
            'pool_timeout': db_cfg['background_pool_timeout'],
        },
    }

    # Installed BEFORE csrf.init_app() below, deliberately: before_request functions run in
    # registration order, and an unauthenticated POST with no session has no CSRF token
    # either, so if CSRF ran first it would answer with "CSRF token missing" - true, but
    # not the real reason, and not what util.js's 401+X-Auth-Required handling looks for
    # (CLAUDE.md 'failure paths must be observable': name the actual reason). Registering
    # here means the auth gate always gets first refusal, so a session-less request is
    # denied for being logged out, not for lacking a token it was never going to have. Safe
    # to install before register_blueprints() has even returned control here - the gate
    # closure reads request.endpoint at REQUEST time, by which point url_map is always
    # fully built (register_blueprints() already ran above), not at registration time.
    from .auth import install as install_auth_gate
    install_auth_gate(app)

    # CSRF: app-wide token check on every POST/PUT/DELETE/PATCH (Flask-WTF).
    # No token expiry - guide/dashboard tabs legitimately sit open for days, and the
    # default 3600s limit would start rejecting their fetches mid-recording.
    app.config['WTF_CSRF_TIME_LIMIT'] = None
    # SSL_STRICT compares Referer against request.host over HTTPS - exactly the check
    # that breaks behind Host-rewriting reverse proxies (dev/changelog/138). The token
    # itself is the protection; keep this off.
    app.config['WTF_CSRF_SSL_STRICT'] = False
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    # Most installs serve plain HTTP on the LAN, where a Secure cookie would never be
    # sent - killing the session, and CSRF validation with it. auth.cookie_secure is the
    # opt-in for an always-HTTPS deployment; it's a RESTART_REQUIRED_KEYS entry because
    # this is read once here, at app build time.
    app.config['SESSION_COOKIE_SECURE'] = bool(auth_cfg.get('cookie_secure'))
    csrf.init_app(app)

    from .auth import wants_json as _wants_json

    @app.errorhandler(CSRFError)
    def _handle_csrf_error(e):
        if _wants_json(request):
            return jsonify({'error': 'CSRF token missing or invalid - reload the page'}), 400
        flash('Security check failed (CSRF token missing or invalid) - please retry.', 'error')
        return redirect(request.referrer or url_for('dashboard.dashboard'))

    if cfg['flask'].get('behind_proxy'):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)

    with app.app_context():
        from . import database  # noqa: F401 - registers models
        from .migrations import is_fresh_db, run_migrations
        # Before the first query of startup, not after it: this arms the pragma listener on
        # every engine without opening a connection, so no connection can be born with
        # SQLite's fail-fast busy_timeout=0 and keep it for its pooled life. _configure_sqlite
        # below still applies them directly, for a connection that got in ahead of this.
        _register_sqlite_pragmas(app.config['SQLITE_CACHE_SIZE_MB'],
                                 app.config['SQLITE_WAL_SIZE_LIMIT_MB'])
        _rename_legacy_account_tables()
        fresh = is_fresh_db()  # must be checked before create_all builds the schema
        tags_table_is_new = not _table_exists('tags')
        db.create_all()
        run_migrations(fresh_db=fresh, cfg=cfg)
        # Unconditional, and it has to be: the FTS5 search indexes are raw virtual tables
        # with no ORM model, so create_all() can't build them, and run_migrations() skips
        # every step on a fresh DB. Without this a brand-new database - and the test
        # suite's schema template - would have no search index tables at all.
        from .search_index import ensure_search_index_schema, reconcile_interrupted_builds
        ensure_search_index_schema()
        # A BUILDING row belongs to a process that died mid-rebuild; nothing else ever
        # clears it, and until something does, every search takes the unindexed scan. Here
        # rather than later: no scheduler job and no request can have started a rebuild yet,
        # so anything found BUILDING is provably stranded (dev/changelog/425).
        reconcile_interrupted_builds()
        # A repair a migration step cannot perform itself: undoing a double-counted health
        # observation is a full replay of the channel's ledger, which is ORM code and only
        # exists here. The step registered the obligation; this discharges it, and does
        # nothing at all on every startup after that (dev/changelog/951).
        from .health_recompute import repair_duplicated_capture_corrections
        repair_duplicated_capture_corrections(cfg)
        _ensure_system_health_job()
        if tags_table_is_new:
            _seed_default_tags()
        _ensure_dvr_dir(cfg)
        _ensure_screenshot_dir(cfg)
        _ensure_live_thumbnail_dir(cfg)
        _configure_sqlite(app.config['SQLITE_CACHE_SIZE_MB'],
                          app.config['SQLITE_WAL_SIZE_LIMIT_MB'])
        _warn_when_pools_are_exhausted(db_cfg)
        _check_config_file_missing(cfg, fresh)
        # Not refresh_auth() - that re-reads config.yaml and would discard config_overrides,
        # per the comment above where auth_cfg was resolved. Both paths hand the same dict
        # to the same reporter instead, so a hand-edited enabled-but-hashless auth block is
        # loud at startup and not only after the next settings save.
        from .auth import report_gate_state
        report_gate_state(auth_cfg, source='startup')
        # Which ffmpeg/ffprobe this install actually resolved, said out loud once per
        # process. Deliberately NOT a startup refusal when one is missing
        # (dev/changelog/910): the app still boots, and the warning goes to the UI rather
        # than to a log nobody is reading. Two subprocess spawns, cached process-wide for
        # the life of the process, so this is paid once and never per request.
        from .toolchain import report_tool_state
        report_tool_state(source='startup',
                          configured_ffmpeg_path=cfg['ffmpeg'].get('path', 'ffmpeg'),
                          configured_ffprobe_path=cfg['ffmpeg'].get('ffprobe_path', ''))
        # Handed the same resolved cfg for the same reason report_gate_state is: a
        # hand-edited config.yaml must be as loud at startup as a settings save is.
        from .proc_utils import report_read_timeout_state
        report_read_timeout_state(cfg, source='startup')

    from .routes import register_blueprints
    register_blueprints(app)

    # Dev-only: expose dev/mockups/ at /mockups/ when explicitly enabled. Gated here
    # (not in register_blueprints) so a deploy with the flag off never has the route.
    if cfg['flask'].get('serve_mockups'):
        from .routes.mockups import mockups_bp
        app.register_blueprint(mockups_bp)
        logging.getLogger(__name__).info('Dev mockups route enabled at /mockups/')

    from .config import is_restart_needed
    from .tz_utils import get_display_tz_name, get_time_format
    from . import health_bands as _health_bands

    # Health-band filters. Pure functions of (score, bands) - the bands come from the
    # `health_bands` context global below, which is resolved once per request, so a filter
    # used inside a `{% for %}` reads no config and touches no disk (CLAUDE.md "No hidden
    # I/O in per-row loops"). dev/changelog/771.
    @app.template_filter('health_css')
    def _health_css(score, bands):
        """The `.hb-*` CSS modifier for a score. None (never tested) gets `hb-none`."""
        if score is None:
            return 'hb-none'
        return f'hb-{_health_bands.band_for(score, bands)}'

    @app.template_filter('health_band_label')
    def _health_band_label(score, bands):
        """The band's label with its configured range, e.g. "Fair (50-79)"."""
        if score is None:
            return _health_bands.UNTESTED_LABEL
        band = _health_bands.band_by_key(bands, _health_bands.band_for(score, bands))
        return band.label if band is not None else ''

    @app.context_processor
    def inject_globals():
        cfg = load_config()
        nav_poll_seconds = cfg['display'].get('nav_poll_interval_seconds', 15)
        bands = _health_bands.resolve_bands(cfg)
        # Sidebar counts (cheap COUNT queries; live/alert counts are refreshed
        # client-side by the /api/nav-status poll after initial render)
        from .database import Recording, UserPref, REC_STATUS_IN_PROGRESS
        nav_recordings_total = db.session.query(db.func.count(Recording.id)).scalar() or 0
        nav_live_count = (db.session.query(db.func.count(Recording.id))
                          .filter(Recording.status == REC_STATUS_IN_PROGRESS).scalar() or 0)
        # Collapsed-sidebar state, server-side so <body class="nav-min"> is in the
        # first paint - localStorage could only apply it after JS runs, which
        # flashes the 212px sidebar on every navigation. One constant query.
        nav_pref = db.session.get(UserPref, 'nav_collapsed')
        try:
            nav_collapsed = bool(json.loads(nav_pref.value)) if nav_pref and nav_pref.value else False
        except ValueError:
            nav_collapsed = False
        return {
            'app_version': __version__,
            'restart_needed': is_restart_needed(),
            'display_timezone': get_display_tz_name(),
            # Resolved once here off the config this processor has already loaded, so every
            # template and every row inside it bands from the same list without a second
            # read. base.html also serializes it for the browser.
            'health_bands': bands,
            'health_bands_json': _health_bands.bands_payload(bands),
            'display_time_format': get_time_format(),
            'nav_poll_interval_ms': int(nav_poll_seconds) * 1000,
            'nav_recordings_total': nav_recordings_total,
            'nav_live_count': nav_live_count,
            'nav_collapsed': nav_collapsed,
            # From current_app.config, not a DB query - no per-request I/O added.
            'auth_enabled': bool((current_app.config.get('AUTH') or {}).get('enabled')),
        }

    if start_scheduler:
        from .scheduler import init_scheduler
        init_scheduler(app)

    return app


def _resolve_secret_key(cfg):
    """Return a real signing key even when config.yaml still has the shipped default.

    Session-signed CSRF tokens are only as strong as the secret key, and the default
    'change-me-in-production' is public. If the user set their own key in config.yaml,
    that wins; otherwise generate one once and persist it to instance/secret_key
    (0600, gitignored) - deliberately NOT written back into config.yaml, where a
    machine-generated secret invites accidental commits/paste-shares.
    """
    configured = cfg['flask'].get('secret_key')
    if configured and configured != 'change-me-in-production':
        return configured
    import secrets
    key_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'secret_key')
    try:
        with open(key_path, 'r') as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(key)
    logging.getLogger(__name__).info(
        'flask.secret_key is the shipped default - generated a persistent key at %s', key_path)
    return key


def _setup_logging(cfg):
    level = getattr(logging, cfg['logging'].get('level', 'INFO').upper(), logging.INFO)
    log_file = cfg['logging'].get('file')
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        # Rotate to keep dvr.log bounded on long-running installs. max_bytes <= 0
        # opts out (plain FileHandler, grows forever) for anyone who rotates externally.
        max_bytes = cfg['logging'].get('max_bytes', 10485760)
        if max_bytes and max_bytes > 0:
            handlers = [logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=cfg['logging'].get('backup_count', 5),
            )]
        else:
            handlers = [logging.FileHandler(log_file)]
    else:
        handlers = [logging.StreamHandler()]
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=handlers,
    )

    # Attached per-handler, not to the root logger: a logger's filters only run for records
    # logged directly to it, so a root-logger filter would miss everything propagating up.
    from .url_utils import CredentialMaskingFilter
    cred_filter = CredentialMaskingFilter()
    for handler in handlers:
        handler.addFilter(cred_filter)

    # Intercept ERROR+ log records and create in-app alerts.
    # The _in_alert guard prevents recursive calls if create_alert itself logs an error.
    _in_alert = threading.local()

    class _AlertHandler(logging.Handler):
        def emit(self, record):
            if getattr(_in_alert, 'active', False):
                return
            # A call site that already raised its own typed alert for this condition marks
            # its record with extra={'already_alerted': True}, and one failure must not
            # produce two alert rows. The record still logs at ERROR - the level describes
            # the log, the marker describes the alert - and what still reaches LOG_ERROR
            # here is an unexpected error no site knew to alert on (dev/changelog/930).
            if getattr(record, 'already_alerted', False):
                return
            _in_alert.active = True
            try:
                from .alerts import create_alert
                alert_type = 'LOG_CRIT' if record.levelno >= logging.CRITICAL else 'LOG_ERROR'
                create_alert(
                    alert_type,
                    title=record.getMessage()[:255],
                    body=self.format(record),
                    source=record.name,
                    recording_id=getattr(record, 'recording_id', None),
                )
            finally:
                _in_alert.active = False

    alert_handler = _AlertHandler()
    alert_handler.setLevel(logging.ERROR)
    # Credentials must be masked before this handler runs - its body goes out to external
    # push services via create_alert() -> enqueue_push().
    alert_handler.addFilter(cred_filter)
    logging.getLogger().addHandler(alert_handler)


def _register_sqlite_pragmas(cache_size_mb, wal_size_limit_mb):
    """Arm WAL, a busy timeout, the page cache and the WAL size limit on every connection.

    Both engines, not just the default one: the background pool talks to the same dvr.db and
    carries the account sync and the index rebuild, so leaving it on SQLite's fail-fast
    busy_timeout=0 would put the fail-fast default on exactly the writers that most need to
    wait one out. APScheduler's separate jobstore engine gets the same treatment in
    scheduler.py, so no writer against dvr.db is left unconfigured.
    """
    from .db_utils import register_sqlite_pragmas
    for engine in db.engines.values():
        register_sqlite_pragmas(engine, cache_size_mb=cache_size_mb,
                                wal_size_limit_mb=wal_size_limit_mb)


def _configure_sqlite(cache_size_mb, wal_size_limit_mb):
    """Apply the pragmas directly to each engine's pooled connection.

    _register_sqlite_pragmas above already armed the listener for connections opened from
    here on; this covers one that was opened before it.
    """
    from .db_utils import configure_sqlite_pragmas
    for engine in db.engines.values():
        configure_sqlite_pragmas(engine, cache_size_mb=cache_size_mb,
                                 wal_size_limit_mb=wal_size_limit_mb)


def _warn_when_pools_are_exhausted(db_cfg):
    """Make a full pool say so, in its own name, at the moment it fills.

    Both pools, because either running dry is a real event with a different meaning: the UI
    pool full means requests are about to queue, the background pool full means a sync or a
    rebuild is about to. Neither had any voice of its own before dev/changelog/423.
    """
    from .db_utils import BACKGROUND_BIND, warn_when_pool_is_exhausted
    warn_when_pool_is_exhausted(
        db.engines[None], 'UI',
        db_cfg['pool_size'] + db_cfg['max_overflow'])
    warn_when_pool_is_exhausted(
        db.engines[BACKGROUND_BIND], 'background',
        db_cfg['background_pool_size'] + db_cfg['background_max_overflow'])


def _rename_legacy_account_tables():
    """One-time rename: xtream_accounts/xtream_sync_logs -> accounts/account_sync_logs.

    Must run before db.create_all() - otherwise SQLAlchemy creates fresh empty
    tables under the new names while the old, populated tables are still present.
    No-op on a fresh install (old tables don't exist yet) or after the first run
    (new tables already exist).
    """
    conn = db.engine.raw_connection()
    try:
        cur = conn.cursor()
        tables = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if 'xtream_accounts' in tables and 'accounts' not in tables:
            cur.execute('ALTER TABLE xtream_accounts RENAME TO accounts')
        if 'xtream_sync_logs' in tables and 'account_sync_logs' not in tables:
            cur.execute('ALTER TABLE xtream_sync_logs RENAME TO account_sync_logs')
        conn.commit()
    finally:
        conn.close()


def _table_exists(table_name: str) -> bool:
    conn = db.engine.raw_connection()
    try:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _ensure_system_health_job():
    """Guarantee the pinned 'TV Guide Channels' system OnDemandTestJob row exists, and
    strip the legacy channel_testing schedule keys from config.yaml once it does.

    Migration _m004 creates the row for existing DBs (copying the legacy keys' values),
    but fresh DBs skip migration steps entirely - this covers them with defaults (fresh
    installs have no customized schedule to preserve).

    The key removal can't be a CONFIG_MIGRATIONS step: migrate_config() runs before
    run_migrations() in create_app(), so a config migration would delete the values
    before _m004 could copy them into the row. Removing them here - after migrations,
    idempotently - also self-heals a restored old config backup that reintroduces them.

    Startup-only commit, deliberately outside retry_on_locked (single-threaded, no
    concurrent writer exists yet - same exemption as _seed_default_tags).
    """
    from .config import config_write_lock, _load_config_file, _write_config_file
    from .database import ChannelGroup, OnDemandTestJob
    log = logging.getLogger(__name__)

    # The job's channel list is its group's membership; the system group's membership
    # is computed at run/display time (channel_groups.check_target_channels), so the
    # group row exists but never gets membership rows. Self-heals a system job left
    # unlinked (group_id None) as well as a fresh DB with neither row.
    system_group = ChannelGroup.query.filter_by(is_system=True).first()
    if system_group is None:
        system_group = ChannelGroup(name='TV Guide Channels', is_system=True)
        db.session.add(system_group)
        db.session.commit()
        log.info('Created system group: TV Guide Channels')

    job = OnDemandTestJob.query.filter_by(is_system=True).first()
    if job is None:
        db.session.add(OnDemandTestJob(
            name='TV Guide Channels',
            is_system=True,
            status='SCHEDULED',
            recurring=True,
            recur_day=0,
            recur_hour=2,
            recur_minute=0,
            recur_paused=False,
            group_id=system_group.id,
        ))
        db.session.commit()
        log.info('Created system health-check job: TV Guide Channels')
    elif job.group_id is None:
        job.group_id = system_group.id
        db.session.commit()

    # Read through the mtime cache, never a direct yaml.safe_load: an uncached parse here
    # cost every create_app() ~13ms (see app/config.py::_load_config_file). The deep copy it
    # returns is what makes mutating `raw` and rewriting the file below safe. The whole
    # read-modify-write is one unit under config.yaml's own write lock, and the write goes
    # through its one atomic writer - this is a rewrite of the live file like any other,
    # and startup being single-threaded in the past is not a property to keep relying on
    # (dev/changelog/671).
    with config_write_lock:
        raw = _load_config_file(round_trip=True)
        if raw is not None:
            ct = raw.get('channel_testing')
            if isinstance(ct, dict):
                removed = [k for k in ('enabled', 'schedule_hour', 'test_days') if k in ct]
                for k in removed:
                    del ct[k]
                if removed:
                    _write_config_file(raw)
                    log.info('Removed legacy channel_testing schedule keys from config.yaml: %s '
                             '(schedule now lives on the TV Guide Channels health check)',
                             ', '.join(removed))


def _seed_default_tags():
    """One-time seed of the two built-in tags on first creation of the tags table.

    Only runs when the tags table itself didn't exist before this startup's db.create_all()
    - never re-seeds just because the table is empty, so a user who deletes both built-ins
    on purpose doesn't get them back on the next restart.

    Startup-only commits (here and in _migrate_db) are deliberately outside
    retry_on_locked: they run single-threaded before any concurrent writer exists.
    """
    from .database import Tag, TagPattern
    log = logging.getLogger(__name__)
    live = Tag(name='live', color='#f85149')
    live.patterns.append(TagPattern(pattern='ᴸᶦᵛᵉ'))
    new = Tag(name='new', color='#3fb950')
    new.patterns.append(TagPattern(pattern='ᴺᵉʷ'))
    db.session.add(live)
    db.session.add(new)
    db.session.commit()
    log.info('Seeded default tags: live, new')


def _ensure_dvr_dir(cfg):
    dvr_dir = cfg['recording']['dvr_output_dir']
    probe = probe_dir(dvr_dir)
    if probe.outcome == PATH_OK:
        return
    if probe.outcome == PATH_MISSING:
        logging.warning(
            'DVR output directory %s does not exist. '
            'Create it with: sudo mkdir -p %s && sudo chown $USER:$USER %s',
            dvr_dir, dvr_dir, dvr_dir,
        )
        return
    # Anything else means the path is there and the problem is elsewhere - creating it
    # is not the fix, and telling the operator to mkdir it is actively misleading. A
    # stale mount is the case this was written for (dev/changelog/723).
    logging.warning('DVR output directory %s', describe_dir_problem(dvr_dir, probe))


def _ensure_screenshot_dir(cfg):
    shot_dir = cfg.get('channel_testing', {}).get(
        'screenshot_dir', '/dvr/channel_test_screenshots'
    )
    if not os.path.isdir(shot_dir):
        try:
            os.makedirs(shot_dir, exist_ok=True)
            logging.info('Created channel test screenshot directory: %s', shot_dir)
        except OSError as exc:
            logging.warning('Could not create screenshot directory %s: %s', shot_dir, exc)


def _ensure_live_thumbnail_dir(cfg):
    thumb_dir = cfg.get('recording', {}).get('live_thumbnail', {}).get(
        'dir', '/dvr/live_thumbnails'
    )
    if not os.path.isdir(thumb_dir):
        try:
            os.makedirs(thumb_dir, exist_ok=True)
            logging.info('Created live thumbnail directory: %s', thumb_dir)
        except OSError as exc:
            logging.warning('Could not create live thumbnail directory %s: %s', thumb_dir, exc)


def _check_config_file_missing(cfg: dict, fresh: bool):
    """Make a missing config.yaml loud when it looks like an established install, instead
    of letting it be silently absorbed as "run on defaults" (dev/docs/BUGS.md 2026-07-18).

    A fresh clone with no config.yaml and no history is deliberate, documented behavior
    (changelog/185) and must stay silent. `fresh` is `is_fresh_db()`, captured by the
    caller *before* db.create_all() ran - checking dvr.db's existence after create_all()
    would be too late, since create_all() itself creates the file.
    """
    from .config import _CONFIG_PATH
    if os.path.exists(_CONFIG_PATH):
        return

    backups_exist = False
    if fresh:
        from .config_backup import get_backup_dir
        backup_dir = get_backup_dir(cfg)
        try:
            backups_exist = any(
                name.endswith('-channelbin-config-backup.yaml')
                for name in os.listdir(backup_dir)
            )
        except OSError:
            backups_exist = False

    if fresh and not backups_exist:
        return  # fresh install, no file, no history - deliberate (changelog/185)

    evidence = 'an existing database' if not fresh else 'existing config backups'
    log = logging.getLogger(__name__)
    log.error(
        'config.yaml is missing but this looks like an established install (%s found) - '
        'running on pure defaults, which silently reverts any custom output directory or '
        'post-processing settings, and drops file logging + notification tokens, on the '
        'next restart. Restore config.yaml from a backup.',
        evidence,
    )
    from .alerts import create_alert
    create_alert(
        'CONFIG_FILE_MISSING',
        title='config.yaml is missing',
        body=(
            'This looks like an established install (%s found), but config.yaml was not '
            'found on disk at startup. The app is running on pure defaults: any custom '
            'output directory, post-processing, or notification settings revert to their '
            'built-in defaults until config.yaml is restored. Check instance/config-backups/ '
            'for a recent backup.' % evidence
        ),
        source='startup',
    )
