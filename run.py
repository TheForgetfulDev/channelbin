import logging
import signal
import sys

from app import create_app
from app.config import load_config
from app.recorder import kill_all_active
from app.postprocessor import kill_active_conversions
from app.concatenator import kill_active_joins
from app.preview import kill_all_previews
from app.scheduler import release_pidfile

app = create_app()


def _handle_shutdown(signum, frame):
    # Kill any live ffmpeg children before this process exits, so restarts
    # (restart.sh sends SIGTERM) never leave an orphaned ffmpeg writing to a
    # segment file the next process doesn't know about. Conversions get the same
    # treatment; the killed conversion's row stays CONVERTING and the startup
    # resume path re-launches it (counting the restart-kill against its budget).
    #
    # The join is killed on the same terms but loses its half-written output, because
    # unlike a conversion it keeps no checkpoint and always re-runs from the top - so
    # the partial is referenced by nothing and would only push the next attempt onto a
    # `_2` name (dev/changelog/986).
    #
    # A live preview is killed on the same terms: its ffmpeg holds a provider connection
    # open for a viewer this process can no longer serve (dev/changelog/1018).
    kill_all_active()
    kill_active_conversions()
    kill_active_joins()
    kill_all_previews()
    release_pidfile()
    sys.exit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    cfg = load_config()
    host = cfg['flask']['host']
    port = cfg['flask']['port']
    # Single-fire (use_reloader=False, one process) - make the LAN-exposure of the
    # default 0.0.0.0 bind obvious. gate_note reflects the optional password gate
    # (app/auth.py) rather than asserting "no auth" unconditionally, which would be
    # false once it's enabled - a door, not internet-grade auth, but still a real one.
    auth_active = bool(cfg.get('auth', {}).get('enabled')) and bool(cfg.get('auth', {}).get('password_hash'))
    gate_note = 'password gate is on' if auth_active else 'no auth'
    if host in ('0.0.0.0', '', '::'):
        logging.getLogger(__name__).info(
            'Listening on %s:%s - reachable from any device on this network (%s)',
            host, port, gate_note)
    else:
        logging.getLogger(__name__).info('Listening on %s:%s', host, port)
    app.run(
        host=host,
        port=port,
        debug=cfg['flask']['debug'],
        use_reloader=False,  # reloader breaks APScheduler and watchdog threads
        threaded=True,       # avoid blocking other requests during synchronous work (e.g. live-thumbnail capture)
    )
