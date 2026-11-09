"""A local stand-in for TheSportsDB's free API, over real HTTP.

The field names and odd shapes are TheSportsDB's own, as measured for
dev/docs/DESIGN-sports-data.md 2: `{"events":null}` for nothing, a 429 with a short non-JSON body, a
null `strTimestamp`, rounds that are not consecutive. The data is made up: invented teams and events
and no image URLs, because nothing received from TheSportsDB ships (dev/changelog/1444).
"""
import json
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LEAGUE_ID = 9001
FIELD_LEAGUE_ID = 9002


def game(event_id, home, away, when, *, league=LEAGUE_ID, round_no=9, season='2099', status='NS',
         home_id=None, away_id=None, **extra):
    """One made-up matchup, spelled the way the API spells it. `when` is UTC."""
    row = {
        'idEvent': str(event_id), 'strEvent': f'{home} vs {away}', 'strSport': 'Pretend Ball',
        'idLeague': str(league), 'strLeague': 'Pretend League', 'strSeason': season,
        'strHomeTeam': home, 'strAwayTeam': away, 'idHomeTeam': str(home_id or zlib.crc32(home.encode()) % 100000 + 1),
        'idAwayTeam': str(away_id or zlib.crc32(away.encode()) % 100000 + 1), 'intRound': str(round_no),
        'strTimestamp': when, 'dateEvent': (when or '2099-01-01')[:10], 'dateEventLocal': (when or '2099-01-01')[:10],
        'strTime': (when or 'T00:00:00')[11:19], 'strVenue': f'{home} Arena', 'strCity': 'Pretendville',
        'strStatus': status, 'strPostponed': 'no',
    }
    row.update(extra)
    return row


def race(event_id, name, when, *, local_date=None, league=FIELD_LEAGUE_ID, round_no=30, season='2099', **extra):
    """One made-up field event: no teams at all, which is how the API spells a race."""
    row = {
        'idEvent': str(event_id), 'strEvent': name, 'strSport': 'Motorsport', 'idLeague': str(league),
        'strLeague': 'Pretend Racing', 'strSeason': season, 'strHomeTeam': None, 'strAwayTeam': None,
        'idHomeTeam': None, 'idAwayTeam': None, 'intRound': str(round_no), 'strTimestamp': when,
        'dateEvent': (when or '')[:10], 'dateEventLocal': local_date or (when or '')[:10],
        'strTime': (when or 'T00:00:00')[11:19], 'strVenue': 'Pretend Speedway', 'strCity': 'Pretendville',
        'strStatus': '', 'strPostponed': 'no',
    }
    row.update(extra)
    return row


def league_row(tsdb_id=LEAGUE_ID, name='Pretend League', sport='Pretend Ball', season='2099',
               alternate='PL, Pretendia', country='Pretendia'):
    return {'idLeague': str(tsdb_id), 'strLeague': name, 'strSport': sport, 'strCurrentSeason': season,
            'strLeagueAlternate': alternate, 'strCountry': country}


def team_row(tsdb_id, name, alternate='', league=LEAGUE_ID, league_name='Pretend League', sport='Pretend Ball'):
    return {'idTeam': str(tsdb_id), 'strTeam': name, 'strTeamAlternate': alternate, 'strTeamShort': name[:3].upper(),
            'idLeague': str(league), 'strLeague': league_name, 'strSport': sport,
            'strLocation': 'Britannia Rd, Pretendville, Pretendia'}


class FakeSportsDB:
    """Holds what each endpoint answers, and records every call. `fault` makes the next calls fail
    the ways the real API has: 'rate_limited' (HTTP 429 with a non-JSON body), 'refused' (HTTP 503),
    'not_json' (HTTP 200 with a non-JSON body)."""

    def __init__(self):
        self.leagues = {LEAGUE_ID: league_row(), FIELD_LEAGUE_ID: league_row(
            FIELD_LEAGUE_ID, 'Pretend Racing', 'Motorsport', alternate='', country='')}
        self.rounds = {}     # (league, season, round) -> [rows]
        self.next_event = {}  # league -> [rows]
        self.teams = {}      # id -> row
        self.team_search = {}  # lowercased name -> [rows]
        # The image host: path -> (status, body bytes, content type, extra headers, send a length). Images are made
        # up by the test (tests/support/images.py), never TheSportsDB's (dev/changelog/1465).
        self.images = {}
        self.calls = []
        self.fault = None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, ctype='application/json', headers=None, length=True):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(status)
                self.send_header('Content-Type', ctype)
                if length:
                    self.send_header('Content-Length', str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the app stopped reading on purpose: an image over its size cap

            def do_GET(self):
                url = urlparse(self.path)
                if url.path.startswith('/images/'):
                    fake.calls.append(('image', {}, url.path))
                    if fake.fault == 'rate_limited':
                        return self._send(429, 'error code: 1015', 'text/plain')
                    status, body, ctype, headers, length = fake.images.get(
                        url.path, (404, '<html>Not Found</html>', 'text/html', None, True))
                    return self._send(status, body, ctype, headers, length)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                endpoint = url.path.rsplit('/', 1)[-1].replace('.php', '')
                fake.calls.append((endpoint, query, url.path))
                if fake.fault == 'rate_limited':
                    return self._send(429, 'error code: 1015', 'text/plain')
                if fake.fault == 'refused':
                    return self._send(503, '<html>Service Unavailable</html>', 'text/html')
                if fake.fault == 'not_json':
                    return self._send(200, '<html>Hello</html>', 'text/html')
                return self._send(200, json.dumps(fake.answer(endpoint, query)))

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = f'http://127.0.0.1:{self._server.server_address[1]}/api/v1/json'
        self.image_base = f'http://127.0.0.1:{self._server.server_address[1]}/images/media/event'

    def image(self, name, body, ctype='image/jpeg', status=200, headers=None, length=True) -> str:
        """Serve `body` as an event image and return its address. `length=False` sends no
        Content-Length, so the body ends when the connection closes."""
        self.images[f'/images/media/event/{name}'] = (status, body, ctype, headers, length)
        return f'{self.image_base}/{name}'

    def answer(self, endpoint, query):
        if endpoint == 'lookupleague':
            row = self.leagues.get(int(query['id']))
            return {'leagues': [row] if row else None}
        if endpoint == 'eventsnextleague':
            return {'events': self.next_event.get(int(query['id'])) or None}
        if endpoint == 'eventsround':
            rows = self.rounds.get((int(query['id']), query['s'], int(query['r'])))
            return {'events': rows or None}
        if endpoint == 'lookupteam':
            row = self.teams.get(int(query['id']))
            return {'teams': [row] if row else None}
        if endpoint == 'searchteams':
            return {'teams': self.team_search.get(query['t'].lower()) or None}
        raise AssertionError(f'the app called {endpoint}, which the fake does not answer')

    def calls_to(self, endpoint):
        return [c for c in self.calls if c[0] == endpoint]

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
