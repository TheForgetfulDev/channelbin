"""Container HEALTHCHECK: is the web app answering?

Any HTTP status counts as healthy. `/` returns 200 normally, redirects to `/login` when
the password gate is on, and `/login` itself 404s when it is off (app/routes/auth.py), so
a status code says nothing about liveness - only a refused connection, a DNS/socket error
or a timeout does.

The port comes from load_config(), the app's one config reader, so a user who moved
flask.port does not silently get a permanently unhealthy container.
"""
import sys
import urllib.error
import urllib.request

sys.path.insert(0, '/app')

from app.config import load_config  # noqa: E402 - after the sys.path insert


def main() -> int:
    port = load_config()['flask'].get('port', 5000)
    try:
        urllib.request.urlopen('http://127.0.0.1:%s/' % port, timeout=8)
    except urllib.error.HTTPError:
        return 0  # answered, just not with a 2xx
    except OSError:
        return 1  # refused, timed out, or the socket died
    return 0


if __name__ == '__main__':
    sys.exit(main())
