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


def team_row(tsdb_id, name, alternate='', league=LEAGUE_ID, league_name='Pretend League', sport='Pretend Ball',
             colors=('', '', ''), short=None, badge=None):
    """One team as lookupteam spells it (measured 2026-11-13, DESIGN-sports-data.md 2): every key is
    sent, a missing color or short name is '', and colors are `#RRGGBB` in either case ('#EF0107',
    '#fbffff'). The default has no colors and no badge, like Forfar Athletic; `badge` is an address
    from FakeSportsDB.badge()."""
    c1, c2, c3 = (tuple(colors) + ('', '', ''))[:3]
    return {'idTeam': str(tsdb_id), 'strTeam': name, 'strTeamAlternate': alternate,
            'strTeamShort': name[:3].upper() if short is None else short,
            'idLeague': str(league), 'strLeague': league_name, 'strSport': sport,
            'strLocation': 'Britannia Rd, Pretendville, Pretendia',
            'strColour1': c1, 'strColour2': c2, 'strColour3': c3, 'strBadge': badge, 'strLogo': None}


class FakeSportsDB:
    """Holds what each endpoint answers, and records every call. `fault` makes the next calls fail
    the ways the real API has: 'rate_limited' (HTTP 429 with a non-JSON body and a `Retry-After`
    of `retry_after`), 'refused' (HTTP 503),
    'not_json' (HTTP 200 with a non-JSON body).

    A round longer than `round_cap` comes back cut to that many rows with nothing saying so, as the
    free key does at 50 (measured 2026-11-14 on NCAA Division 1 Football, dev/changelog/1549).
    `round_pick(rows, n)` chooses which rows the n-th call of that round gets, since the real
    answer's 50 change from call to call; by default the first ones. lookupevent and searchevents
    find a game in any round, cut or not, or in `games`, which holds those in no round."""

    ROUND_CAP = 50

    def __init__(self):
        self.leagues = {LEAGUE_ID: league_row(), FIELD_LEAGUE_ID: league_row(
            FIELD_LEAGUE_ID, 'Pretend Racing', 'Motorsport', alternate='', country='')}
        self.rounds = {}     # (league, season, round) -> [rows]
        self.next_event = {}  # league -> [rows]
        self.teams = {}      # id -> row
        self.team_search = {}  # lowercased name -> [rows]
        self.games = {}      # event id -> row, for a game no round lists
        self.round_cap = self.ROUND_CAP
        self.round_pick = None
        self._round_calls = {}
        # The image host: path -> (status, body bytes, content type, extra headers, send a length). Images are made
        # up by the test (tests/support/images.py), never TheSportsDB's (dev/changelog/1465).
        self.images = {}
        self.calls = []
        self.fault = None
        self.retry_after = '120'  # a 429's Retry-After header; None sends none
        self.answer_overrides = {}  # endpoint -> the whole JSON answer, whatever was asked
        self.clock = None  # what eventsnext counts as now, UTC 'YYYY-MM-DDTHH:MM:SS'
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
                    # The header the real one sent (2026-11-14, dev/changelog/1551).
                    return self._send(429, 'error code: 1015', 'text/plain',
                                      headers={'Retry-After': fake.retry_after} if fake.retry_after else None)
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

    def badge(self, name, body, ctype='image/png', status=200) -> str:
        """Serve `body` as a team badge and return its address."""
        self.images[f'/images/media/team/badge/{name}'] = (status, body, ctype, None, True)
        return f'{self.image_base.rsplit("/", 1)[0]}/team/badge/{name}'

    def answer(self, endpoint, query):
        if endpoint in self.answer_overrides:
            return self.answer_overrides[endpoint]
        if endpoint == 'lookupleague':
            row = self.leagues.get(int(query['id']))
            return {'leagues': [row] if row else None}
        if endpoint == 'eventsnextleague':
            return {'events': self.next_event.get(int(query['id'])) or None}
        if endpoint == 'eventsround':
            key = (int(query['id']), query['s'], int(query['r']))
            rows = list(self.rounds.get(key) or [])
            n = self._round_calls[key] = self._round_calls.get(key, 0) + 1
            if len(rows) > self.round_cap:
                rows = self.round_pick(rows, n) if self.round_pick else rows
                rows = rows[:self.round_cap]
            return {'events': rows or None}
        if endpoint == 'lookupevent':
            row = self._every_game().get(str(query['id']))
            return {'events': [row] if row else None}
        if endpoint == 'searchevents':
            # Exact name, home team first, underscores for spaces; one row at most. The key is
            # `event`, singular (measured 2026-11-14). What a miss looks like was not measured: null,
            # as every other empty answer is, is assumed. `d` matches dateEvent, the UTC date. By
            # season alone the real one gave a Feb 13 NBA game for a pair that also meets in
            # November (measured 2026-11-14); how it picks was not measured, and here it is the latest.
            name = query['e'].replace('_', ' ')
            found = [r for r in self._every_game().values()
                     if r.get('strEvent') == name and ('s' not in query or r.get('strSeason') == query['s'])
                     and ('d' not in query or r.get('dateEvent') == query['d'])]
            found.sort(key=lambda r: r.get('strTimestamp') or '', reverse=True)
            return {'event': found[:1] or None}
        if endpoint == 'eventsnext':
            # A team's next game, home or away, in any league, one row: UTEP and Wyoming both gave the
            # UTEP game, and the Nuggets an away game (measured 2026-11-14, dev/changelog/1553). The
            # docs' "home games only" is wrong. Next means after `clock` (UTC, 'YYYY-MM-DDTHH:MM:SS')
            # when a test sets it, else any game not finished.
            team = str(query['id'])
            ahead = [r for r in self._every_game().values()
                     if team in (r.get('idHomeTeam'), r.get('idAwayTeam')) and r.get('strStatus') != 'FT'
                     and (self.clock is None or (r.get('strTimestamp') or '') > self.clock)]
            ahead.sort(key=lambda r: r.get('strTimestamp') or '')
            return {'events': ahead[:1] or None}
        if endpoint == 'lookupteam':
            row = self.teams.get(int(query['id']))
            return {'teams': [row] if row else None}
        if endpoint == 'searchteams':
            return {'teams': self.team_search.get(query['t'].lower()) or None}
        raise AssertionError(f'the app called {endpoint}, which the fake does not answer')

    def _every_game(self) -> dict:
        out = {r['idEvent']: r for rows in self.rounds.values() for r in rows}
        out.update({str(k): v for k, v in self.games.items()})
        return out

    def calls_to(self, endpoint):
        return [c for c in self.calls if c[0] == endpoint]

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
