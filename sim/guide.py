"""Guide programs, generated per station per day-long block.

A station's schedule is cut into 24-hour blocks that start at a time of day of the station's
own, so programs cross midnight the way real ones do and no two stations change blocks at the
same moment. Each block is drawn from a generator seeded by (world seed, station, block), so
any window of the schedule can be produced on its own, and a re-sync a day later sees exactly
the programs it saw before for every hour the two windows share.
"""
import datetime as _dt

from .names import invented_stem
from .rand import derive, percentile_sampler, rng

BLOCK = _dt.timedelta(days=1)
_EPOCH = _dt.datetime(2000, 1, 1)

_ADJ = ('Silent', 'Golden', 'Hidden', 'Northern', 'Last', 'Broken', 'Bright', 'Wild', 'Lost',
        'Second', 'Iron', 'Paper', 'Quiet', 'Open', 'Crimson', 'Distant', 'Winter', 'Summer')
_NOUN = ('Harbor', 'Valley', 'Signal', 'Road', 'Garden', 'River', 'Station', 'Island', 'Bridge',
         'Kitchen', 'Frontier', 'Promise', 'Circle', 'Window', 'Lantern', 'Orchard', 'Mile',
         'Shore', 'Field', 'Workshop')
_SHOW_FORMS = ('The {a} {n}', '{n} {n2}', '{a} {n}', 'Life on the {n}', 'Inside the {n}',
               'The {n} Show', '{n} Stories')
_NEWS = ('Morning News', 'Midday Report', 'Evening News', 'World Report', 'News at Ten',
         'Late Edition', 'Business Today', 'Weekend Update', 'Headline Hour', 'Live Desk')
_SPORTS_SHOWS = ('Sports Tonight', 'The Final Whistle', 'Matchday Live', 'Scoreboard',
                 'Pre-Game Show', 'Post-Game Analysis', 'Highlights Hour')
_KIDS = ('{n} Friends', 'The Little {n}', 'Adventures in the {n}', '{a} Pals', 'Tiny {n}')
_FACTUAL = ('Secrets of the {n}', 'The {a} Coast', 'Great {n}s', 'How It Works: {n}',
            'Wild {n}', 'Taste of the {n}', 'Building the {n}')
_FILLER = ('Programming Information', 'Off Air', 'Station Break', 'Coming Up')
_RATINGS = ('TV-G', 'TV-PG', 'TV-14', 'TV-MA', 'TV-Y7')
_SENTENCE = ('A {a} discovery changes everything at the {n}.',
             'Old friends meet again by the {n}, and not everyone is glad.',
             'The team races to finish the {n} before the {a} season ends.',
             'An unexpected visitor arrives with news about the {n}.',
             'Plans for the {n} fall apart when a {a} secret comes out.',
             'Highlights, interviews and analysis from around the {n}.',
             'A look at the people who keep the {n} running.')

_MOVIE_GENRES = ('Movies', 'Cinema', 'Action', 'Classic', 'Drama', 'Comedy')
_FACTUAL_GENRES = ('Docs', 'Nature', 'Travel', 'Food', 'Life')


def _fill(r, form: str) -> str:
    return form.format(a=r.choice(_ADJ), n=r.choice(_NOUN), n2=r.choice(_NOUN))


def movie_title(r) -> str:
    return _fill(r, r.choice(('The {a} {n}', '{a} {n}', 'The {n} of {n2}', 'Return to the {n}',
                              'Beyond the {n}', '{n}fall')))


def team_name(r) -> str:
    return invented_stem(r)


class Program:
    __slots__ = ('start', 'stop', 'title', 'sub_title', 'desc', 'episode', 'icon', 'new',
                 'rating', 'category')

    def __init__(self, start, stop, title):
        self.start, self.stop, self.title = start, stop, title
        self.sub_title = self.desc = self.episode = self.icon = self.rating = None
        self.category = None
        self.new = False


def _title(r, genre: str) -> tuple[str, str]:
    """(title, category word) for a new program on a channel of this genre."""
    if genre in ('News', 'Weather', 'Business', 'Local', 'World'):
        return r.choice(_NEWS), 'News'
    if genre == 'Sports':
        if r.random() < 0.5:
            return f'{team_name(r)} at {team_name(r)}', 'Sports event'
        return r.choice(_SPORTS_SHOWS), 'Sports'
    if genre in _MOVIE_GENRES:
        return movie_title(r), 'Movie'
    if genre == 'Kids':
        return _fill(r, r.choice(_KIDS)), 'Children'
    if genre in _FACTUAL_GENRES:
        return _fill(r, r.choice(_FACTUAL)), 'Documentary'
    if genre == 'Music':
        return _fill(r, r.choice(('{a} Sessions', 'Live at the {n}', 'The {n} Countdown'))), 'Music'
    return _fill(r, r.choice(_SHOW_FORMS)), 'Series'


class GuideShape:
    """The calibrated mix one guide draws its programs from."""

    def __init__(self, cal):
        self.minutes = percentile_sampler(cal.program_minutes)
        self.filler = cal.filler_share
        self.long = cal.long_program_share
        self.gap = cal.gap_rate
        self.overlap = cal.overlap_rate
        self.same_title = cal.same_title_rate
        self.fields = dict(cal.field_share)


def block_start(seed, station_key: str, index: int) -> _dt.datetime:
    offset = _dt.timedelta(minutes=15 * (derive(seed, 'guide-block', station_key) % 96))
    return _EPOCH + index * BLOCK + offset


def _block_index(seed, station_key: str, when: _dt.datetime) -> int:
    return (when - block_start(seed, station_key, 0)) // BLOCK


def _block(seed, station, index: int, shape: GuideShape) -> list:
    r = rng(seed, 'guide', station.key, index)
    t = block_start(seed, station.key, index)
    end = t + BLOCK
    out = []
    prev = None
    episode = 0
    f = shape.fields
    while t < end:
        u = r.random()
        if u < shape.filler:
            minutes = r.choice((1, 2))
        elif u < shape.filler + shape.long:
            minutes = r.choice((360, 420, 480))
        else:
            minutes = max(5, int(round(shape.minutes(r) / 5.0)) * 5)
        stop = t + _dt.timedelta(minutes=minutes)
        if stop > end or end - stop < _dt.timedelta(minutes=5):
            stop = end
        if prev is not None and r.random() < shape.same_title:
            title, category = prev.title, prev.category
            episode += 1
        else:
            title, category = _title(r, station.genre)
            episode = 1 + r.randrange(0, 20)
        p = Program(t, stop, title)
        p.category = category if r.random() < f['category'] else None
        if r.random() < f['description']:
            p.desc = _fill(r, r.choice(_SENTENCE))
        if r.random() < f['sub_title']:
            p.sub_title = _fill(r, r.choice(('The {n}', '{a} Night', 'Part of the {n}',
                                             'A {a} Return')))
        if r.random() < f['episode_num']:
            p.episode = (1 + r.randrange(0, 8), episode)
        if r.random() < f['icon']:
            p.icon = f'{derive(seed, "art", station.key, index, len(out)):016x}'
        p.new = r.random() < f['new']
        if r.random() < f['rating']:
            p.rating = r.choice(_RATINGS)
        out.append(p)
        prev = p
        t = stop
    if len(out) > 1:
        for a, b in zip(out, out[1:]):
            u = r.random()
            length = (a.stop - a.start).total_seconds() / 60
            if u < shape.gap and length > 3:
                a.stop -= _dt.timedelta(minutes=min(30, max(2, int(length // 3))))
            elif u < shape.gap + shape.overlap:
                a.stop = b.start + _dt.timedelta(minutes=1 + r.randrange(0, 10))
    return out


def programs(seed, station, start: _dt.datetime, end: _dt.datetime, shape: GuideShape) -> list:
    """Every program of `station` that overlaps [start, end), in start order."""
    first = _block_index(seed, station.key, start)
    last = _block_index(seed, station.key, end)
    out = []
    for index in range(first, last + 1):
        out += [p for p in _block(seed, station, index, shape) if p.stop > start and p.start < end]
    return out
