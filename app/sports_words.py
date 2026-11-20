"""The word for one event of a league: game, match, race or event (dev/changelog/1663).

It comes from the league's sport as TheSportsDB names it and from the event's shape, never from a
named league: every motorsport says race, and the sports that say match are a set here. Read by
every surface that has one league in hand, so a race is never a game on one page and a race on the
next. Where no single league is in hand (a setting for every league, a play's ended-reason, an
alert type's name) the app's word is "game" (dev/docs/DESIGN.md 6).
"""

GAME = ('game', 'games')
MATCH = ('match', 'matches')
RACE = ('race', 'races')
EVENT = ('event', 'events')

#: TheSportsDB's strSport for the sports whose contests are matches.
MATCH_SPORTS = frozenset({'Soccer', 'Tennis', 'Rugby', 'Cricket'})
#: And for those whose events are races, whatever the series.
RACE_SPORTS = frozenset({'Motorsport'})


def noun(sport, matchup=None) -> tuple:
    """(one, many) for a league of `sport`. `matchup` is True when an event in hand is between two
    sides, False when there are events and none is, None with no event to read the shape from."""
    if matchup:
        return MATCH if sport in MATCH_SPORTS else GAME
    if sport in RACE_SPORTS:
        return RACE
    if matchup is False:
        return EVENT
    return MATCH if sport in MATCH_SPORTS else GAME


_MANY = dict((GAME, MATCH, RACE, EVENT))


def many(one: str) -> str:
    """The plural of one of the four words: `matches`, never `matchs`."""
    return _MANY[one]


def teamed(sport) -> str:
    """The word for a contest between two sides in a league of `sport`, whatever else the league
    holds: what a sentence about naming both teams is about."""
    return noun(sport, True)[0]


def a(word: str) -> str:
    """`a game`, `an event`."""
    return f'an {word}' if word[:1] in 'aeiou' else f'a {word}'
