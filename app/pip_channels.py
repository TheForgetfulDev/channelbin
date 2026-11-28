"""Picture-in-picture channels on the output feed (dev/docs/DESIGN-pip-channels.md).

A channel a person makes on the IPTV Output page that shows two pictures: a big one ("main")
and a small one ("inset"). Each picture is a game, a channel, a channel group or a search for
channels by name, and plays exactly what the matching plain play would (section 5). This
module is the only writer of `pip_channels` (its `on_output` aside, which is
output_feed.set_on_output's), and the one place a picture is turned into what it plays:
resolve_feed() for a tune-in, describe() for a page or the guide a player is sent.

What a tune-in gets is output_feed's to run: its two pictures combined on the GPU
(app/pip_engine.py), the big picture alone on a box whose GPU cannot combine them (section
7.3), or, with neither picture able to play, the channel's card, a looped clip saying why
nothing is on (card_for()). Inside a combined picture the big window's card is that same
clip, and the small window's the short words of pane_words().

Every write is one retry_on_locked unit. Ending or moving the plays of a channel that changed
is the route's, after the commit and never inside it (output_feed.stop_pip, refresh_pip,
clear_pip).
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from . import db, output_feed
from . import connection_limits as connlim
from .database import (PIP_SOURCE_CHANNEL, PIP_SOURCE_GAME, PIP_SOURCE_GROUP, PIP_SOURCE_KINDS,
                       PIP_SOURCE_SEARCH, Account, Channel, ChannelGroup, ChannelGroupMember, PipChannel,
                       SportsEvent, SportsLeague)
from .db_utils import retry_on_locked
from .fmt_utils import counted
from .proc_utils import PIP_CORNER_SIZES, PIP_CORNERS, PIP_LAYOUT_CORNER, PIP_LAYOUT_SIDE, PIP_LAYOUTS
from .tz_utils import day_label, format_local, get_display_tz, is_24h, to_local

log = logging.getLogger(__name__)

ROLE_MAIN = 'main'
ROLE_INSET = 'inset'
ROLES = (ROLE_MAIN, ROLE_INSET)

NAME_MAX = 40
QUERY_MAX = 80
#: How many of a search's matches a tune-in asks, in TV Guide order, before it gives up: each is
#: a resolve of its own, and a search for one letter matches most of a guide.
SEARCH_WALK = 20
#: The most coming games the setup dialog is handed.
PICKER_GAMES = 2000
#: The largest id a picture may name: what SQLite stores in an INTEGER. A larger one is no
#: row's id, and binding it raises before any query runs.
REF_MAX = 2 ** 63 - 1

# What one window of a picture-in-picture channel shows (dev/docs/DESIGN-pip-channels.md 11).
# Every branch over a pane's state names each of them.
PANE_LIVE = 'live'                  # playing a channel
PANE_EMPTY = 'empty'                # nothing chosen for it
PANE_BEFORE = 'before'              # its game is not on yet
PANE_NOT_LISTED = 'not_listed'      # its game is near or on, and no guide lists it
PANE_OVER = 'over'                  # its game is over
PANE_GONE = 'gone'                  # its game left the sports schedule, or its channel the TV Guide
PANE_NO_MATCH = 'no_match'          # a search no channel's name matches
PANE_CANNOT = 'cannot'              # it has a channel, and none the output may play right now
PANE_SAME = 'same'                  # it would play the channel the big picture is on
PANE_RECONNECTING = 'reconnecting'  # every channel it could play dropped, and it is trying again
PANE_TAKEN = 'taken'                # a recording took its connection
PANE_YIELDED = 'yielded'            # the big picture took its connection
PANE_BUSY = 'busy'                  # every account it plays from is busy, or no player slot is free
PANE_NO_GPU = 'no_gpu'              # the small picture is not drawn: nothing can combine the two
PANE_STATES = (PANE_LIVE, PANE_EMPTY, PANE_BEFORE, PANE_NOT_LISTED, PANE_OVER, PANE_GONE, PANE_NO_MATCH,
               PANE_CANNOT, PANE_SAME, PANE_RECONNECTING, PANE_TAKEN, PANE_YIELDED, PANE_BUSY, PANE_NO_GPU)

CORNER_WORDS = {'tl': 'Top left', 'tr': 'Top right', 'bl': 'Bottom left', 'br': 'Bottom right'}
SIZE_WORDS = {'s': 'Small', 'm': 'Medium', 'l': 'Large'}


class PipRefused(Exception):
    """A write was refused; `message` is user-facing prose, `status` the HTTP code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Source:
    """What one picture is: a kind (PIP_SOURCE_*) and its `ref` (a game's TheSportsDB event id, a
    channel's id, a group's id) or a search's words."""
    kind: str
    ref: Optional[int] = None
    query: str = ''

    @property
    def key(self) -> tuple:
        if self.kind == PIP_SOURCE_SEARCH:
            return self.kind, self.query.casefold()
        return self.kind, self.ref

    def to_dict(self) -> dict:
        return {'kind': self.kind, 'ref': self.ref, 'query': self.query}


def source_of(row: PipChannel, role: str) -> Optional[Source]:
    if role not in ROLES:
        raise ValueError(f'Unknown picture "{role}"')
    kind = getattr(row, f'{role}_kind')
    if kind is None:
        return None
    if kind not in PIP_SOURCE_KINDS:
        raise ValueError(f'Unknown kind of picture "{kind}" on picture-in-picture channel {row.id}')
    return Source(kind, getattr(row, f'{role}_ref'), getattr(row, f'{role}_query') or '')


def is_set(row: PipChannel) -> bool:
    """Whether both pictures are chosen. A channel with one is not offered a swapped copy and
    plays its card, as one with none does."""
    return row.main_kind is not None and row.inset_kind is not None


def pictures(row: PipChannel, swapped: bool) -> tuple:
    """(big, small) for the channel, or for its swapped copy."""
    main, inset = source_of(row, ROLE_MAIN), source_of(row, ROLE_INSET)
    return (inset, main) if swapped else (main, inset)


@dataclass
class Served:
    """One picture-in-picture channel as the feed lists it: the row, or its swapped copy."""
    row: PipChannel = field(repr=False)
    swapped: bool = False

    @property
    def name(self) -> str:
        return swapped_name(self.row.name) if self.swapped else self.row.name


def swapped_name(name: str) -> str:
    return f'{name} (swapped)'


def served() -> list:
    """Every picture-in-picture channel in the order it was made, each followed by its swapped
    copy when it has one. One query. Whether it is on the output is the row's `on_output`."""
    out = []
    for row in PipChannel.query.order_by(PipChannel.id).all():
        out.append(Served(row))
        if row.swapped_copy and is_set(row):
            out.append(Served(row, True))
    return out


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _clean_source(raw, which: str) -> Source:
    if not isinstance(raw, dict):
        raise PipRefused(f'Choose what the {which} picture shows.')
    kind = raw.get('kind')
    if kind not in PIP_SOURCE_KINDS:
        raise PipRefused(f'Choose what the {which} picture shows.')
    if kind == PIP_SOURCE_SEARCH:
        if not isinstance(raw.get('query') or '', str):
            raise PipRefused(f'Type part of a channel name for the {which} picture\'s search.')
        words = ' '.join((raw.get('query') or '').split())
        if not words:
            raise PipRefused(f'Type part of a channel name for the {which} picture\'s search.')
        if len(words) > QUERY_MAX:
            raise PipRefused(f'A search is at most {QUERY_MAX} characters.')
        return Source(kind, None, words)
    ref = raw.get('ref')
    if ref is None and kind in (PIP_SOURCE_CHANNEL, PIP_SOURCE_GROUP):
        # A picture detach_refs() unlinked, sent back unchanged from the setup dialog.
        what = 'channel group' if kind == PIP_SOURCE_GROUP else 'channel'
        raise PipRefused(f'The {which} picture\'s {what} was removed. Choose another.', 409)
    if isinstance(ref, bool) or not isinstance(ref, int) or not 0 < ref <= REF_MAX:
        raise PipRefused(f'Choose what the {which} picture shows.')
    return Source(kind, ref)


def clean(data: dict) -> dict:
    """A create or a change as it came in, checked and tidied: {'name', 'main', 'inset' (each a
    Source), 'layout', 'corner', 'size', 'swapped_copy'}. Raises PipRefused, in the words the
    setup dialog shows. Reads nothing: whether the pictures exist is check_sources()."""
    if not isinstance(data, dict):
        raise PipRefused('That is not a picture-in-picture channel.')
    if not isinstance(data.get('name') or '', str):
        raise PipRefused('Give it a name.')
    name = ' '.join((data.get('name') or '').split())
    if not name:
        raise PipRefused('Give it a name.')
    if len(name) > NAME_MAX:
        raise PipRefused(f'A name is at most {NAME_MAX} characters.')
    layout = data.get('layout', PIP_LAYOUT_CORNER)
    corner, size = data.get('corner', 'br'), data.get('size', 'm')
    # Strings only: PIP_CORNER_SIZES is a dict, and a list or an object asked of one raises.
    if (not all(isinstance(v, str) for v in (layout, corner, size))
            or layout not in PIP_LAYOUTS or corner not in PIP_CORNERS or size not in PIP_CORNER_SIZES):
        raise PipRefused('That is not a layout a picture-in-picture channel has.')
    if not isinstance(data.get('swapped_copy', False), bool):
        raise PipRefused('Say whether a swapped copy is offered.')
    return {'name': name, 'main': _clean_source(data.get('main'), 'big'),
            'inset': _clean_source(data.get('inset'), 'small'), 'layout': layout, 'corner': corner,
            'size': size, 'swapped_copy': bool(data.get('swapped_copy', False))}


def _exists(source: Source, which: str) -> None:
    if source.kind == PIP_SOURCE_GAME:
        if SportsEvent.query.filter_by(tsdb_id=source.ref).first() is None:
            raise PipRefused(f'The {which} picture\'s game is no longer in the sports schedule. Choose another.', 409)
    elif source.kind == PIP_SOURCE_CHANNEL:
        ch = db.session.get(Channel, source.ref)
        if ch is None or not ch.in_guide:  # guide-flag-read-ok: a picture may be any channel that is its own guide row
            raise PipRefused(f'The {which} picture\'s channel is no longer in your TV Guide. Choose another.', 409)
    elif source.kind == PIP_SOURCE_GROUP:
        group = db.session.get(ChannelGroup, source.ref)
        if group is None or not group.in_guide:
            raise PipRefused(f'The {which} picture\'s channel group is no longer in your TV Guide. '
                             'Choose another.', 409)
    elif source.kind != PIP_SOURCE_SEARCH:
        raise ValueError(f'Unknown kind of picture "{source.kind}"')


def same_now(a: Source, b: Source, cfg: dict, now: Optional[datetime] = None) -> bool:
    """Whether two pictures would play the same channel right now: the same game, channel, group
    or search words, or a game that is on and the channel or group it would be taken from. Two
    games that the same channel lists at different times are two pictures."""
    if a.kind == b.kind:
        return a.key == b.key
    game, other = (a, b) if a.kind == PIP_SOURCE_GAME else (b, a)
    if game.kind != PIP_SOURCE_GAME or other.kind == PIP_SOURCE_SEARCH:
        return False
    pane = resolve_source(game, cfg, feed_id=0, label='a picture', now=now)
    if pane.state != PANE_LIVE:
        return False
    member = pane.play.member
    if other.kind == PIP_SOURCE_CHANNEL:
        return member.id == other.ref
    if other.kind == PIP_SOURCE_GROUP:
        return db.session.query(ChannelGroupMember.id).filter_by(
            group_id=other.ref, channel_id=member.id).first() is not None
    raise ValueError(f'Unknown kind of picture "{other.kind}"')


def check_sources(spec: dict, cfg: dict) -> None:
    """Refuse a create or a change whose pictures no longer exist, or would play the same channel
    (section 5). Reads only; called before the write's retry unit, never inside it."""
    _exists(spec['main'], 'big')
    _exists(spec['inset'], 'small')
    if same_now(spec['main'], spec['inset'], cfg):
        raise PipRefused('The two pictures would play the same channel. Choose something else for one of them.')


def _put(row: PipChannel, spec: dict) -> None:
    row.name = spec['name']
    for role in ROLES:
        source = spec[role]
        setattr(row, f'{role}_kind', source.kind)
        setattr(row, f'{role}_ref', source.ref)
        setattr(row, f'{role}_query', source.query or None)
    row.layout, row.corner, row.size = spec['layout'], spec['corner'], spec['size']
    row.swapped_copy = spec['swapped_copy']
    row.updated_at = datetime.utcnow()


def create(spec: dict) -> PipChannel:
    """A new channel from a clean()ed, check_sources()ed spec, on the output from the start."""
    @retry_on_locked()
    def _create_and_commit():
        # On the output from the start by the column's own default: `on_output` has one writer.
        row = PipChannel(name=spec['name'])
        _put(row, spec)
        db.session.add(row)
        db.session.commit()
        return row.id

    row = db.session.get(PipChannel, _create_and_commit())
    log.info('Picture-in-picture channel %d (%s) created: %s', row.id, row.name, _spec_words(spec))
    return row


def change(row_id: int, spec: dict) -> Optional[PipChannel]:
    """Save a channel's name, pictures and layout. None when the row is gone."""
    @retry_on_locked()
    def _change_and_commit():
        row = db.session.get(PipChannel, row_id)
        if row is None:
            return False
        _put(row, spec)
        db.session.commit()
        return True

    if not _change_and_commit():
        return None
    row = db.session.get(PipChannel, row_id)
    log.info('Picture-in-picture channel %d (%s) changed: %s', row.id, row.name, _spec_words(spec))
    return row


def detach_refs(kind: str, ids) -> set:
    """Unlink every picture of kind `kind` (PIP_SOURCE_CHANNEL or PIP_SOURCE_GROUP) that names
    one of `ids`, which are being deleted: the picture keeps its kind with no ref, which
    resolve_source() and describe() read as removed until a person chooses again. Channel and
    group ids are re-issued, so a ref left behind would play whatever gets the id next
    (dev/docs/BUGS.md 2026-11-25 @ 07:47:14 PM). Mutates without committing, inside the
    caller's delete unit; returns the row ids it unlinked, for refresh_detached() after that
    commit."""
    if kind not in (PIP_SOURCE_CHANNEL, PIP_SOURCE_GROUP):
        raise ValueError(f'A picture of kind "{kind}" names no local id')
    ids = list(ids)
    touched = set()
    if not ids:
        return touched
    for role in ROLES:
        kind_col, ref_col = getattr(PipChannel, f'{role}_kind'), getattr(PipChannel, f'{role}_ref')
        hit = PipChannel.query.filter(kind_col == kind, ref_col.in_(ids))
        touched.update(row_id for (row_id,) in hit.with_entities(PipChannel.id))
        hit.update({f'{role}_ref': None}, synchronize_session=False)
    return touched


def repoint_refs(old_channel_id: int, new_channel_id: int) -> set:
    """Point every picture of the old channel at the channel that replaced it
    (app/channel_successors.py, dev/docs/DESIGN-channel-successors.md section 8.2). A picture
    names one channel, so there is nothing to merge. Mutates without committing, inside the
    accept's unit; returns the row ids it changed, for output_feed.refresh_pip() after that
    commit."""
    touched = set()
    for role in ROLES:
        kind_col, ref_col = getattr(PipChannel, f'{role}_kind'), getattr(PipChannel, f'{role}_ref')
        hit = PipChannel.query.filter(kind_col == PIP_SOURCE_CHANNEL, ref_col == old_channel_id)
        touched.update(row_id for (row_id,) in hit.with_entities(PipChannel.id))
        hit.update({f'{role}_ref': new_channel_id}, synchronize_session=False)
    return touched


def refresh_detached(row_ids, what: str) -> None:
    """After the commit of a delete that detach_refs() unlinked pictures in, the last thing the
    delete does: say so, and refresh each row's live plays (output_feed.refresh_pip), which
    stops a combined play's picture of the gone channel or group and asks it again, onto the
    "removed" card, and leaves a play on its big picture alone as it is until it tunes in
    again. A refresh that fails is logged and never undoes or fails the delete. Requires an
    app context."""
    for row_id in sorted(row_ids):
        row = db.session.get(PipChannel, row_id)
        if row is None:
            continue
        log.info('Picture-in-picture channel %d (%s) lost a picture: its %s was deleted, and the '
                 'picture shows it as removed until another is chosen', row.id, row.name, what)
        try:
            output_feed.refresh_pip(row_id)
        except Exception:                               # noqa: BLE001 - one row's plays, never the delete
            log.exception('Picture-in-picture channel %d: its plays could not be refreshed after the '
                          'delete; they move at their next tune-in', row_id)


def _spec_words(spec: dict) -> str:
    def one(source):
        return f'{source.kind} {source.ref if source.kind != PIP_SOURCE_SEARCH else repr(source.query)}'
    return (f'big {one(spec["main"])}, small {one(spec["inset"])}, {spec["layout"]} {spec["corner"]} '
            f'{spec["size"]}{", with a swapped copy" if spec["swapped_copy"] else ""}')


def swap(row_id: int) -> Optional[PipChannel]:
    """Exchange the two pictures. None when the row is gone; PipRefused with nothing set up."""
    @retry_on_locked()
    def _swap_and_commit():
        row = db.session.get(PipChannel, row_id)
        if row is None:
            return None
        if not is_set(row):
            return False
        for column in ('kind', 'ref', 'query'):
            main, inset = getattr(row, f'main_{column}'), getattr(row, f'inset_{column}')
            setattr(row, f'main_{column}', inset)
            setattr(row, f'inset_{column}', main)
        row.updated_at = datetime.utcnow()
        db.session.commit()
        return True

    done = _swap_and_commit()
    if done is None:
        return None
    if not done:
        raise PipRefused('Set it up first: it has no pictures to swap.', 409)
    log.info('Picture-in-picture channel %d: its two pictures were swapped', row_id)
    return db.session.get(PipChannel, row_id)


def clear(row_id: int) -> Optional[PipChannel]:
    """Take both pictures off and turn the swapped copy off. The row and its number stay
    (section 4.1). None when the row is gone."""
    @retry_on_locked()
    def _clear_and_commit():
        row = db.session.get(PipChannel, row_id)
        if row is None:
            return False
        for role in ROLES:
            for column in ('kind', 'ref', 'query'):
                setattr(row, f'{role}_{column}', None)
        row.swapped_copy = False
        row.updated_at = datetime.utcnow()
        db.session.commit()
        return True

    if not _clear_and_commit():
        return None
    log.info('Picture-in-picture channel %d cleared', row_id)
    return db.session.get(PipChannel, row_id)


def remove(row_id: int) -> Optional[str]:
    """Delete the channel. Its name, or None when the row was already gone. Its id is never
    issued again (AUTOINCREMENT), so a player that kept it finds nothing, never another channel."""
    @retry_on_locked()
    def _remove_and_commit():
        row = db.session.get(PipChannel, row_id)
        if row is None:
            return None
        name = row.name
        db.session.delete(row)
        db.session.commit()
        return name

    name = _remove_and_commit()
    if name is not None:
        log.info('Picture-in-picture channel %d (%s) removed from the output', row_id, name)
    return name


def set_swapped_copy(row_id: int, on: bool) -> Optional[PipChannel]:
    """Offer the swapped copy, or stop. None when the row is gone; PipRefused with nothing set up."""
    @retry_on_locked()
    def _set_and_commit():
        row = db.session.get(PipChannel, row_id)
        if row is None:
            return None
        if on and not is_set(row):
            return False
        row.swapped_copy = bool(on)
        row.updated_at = datetime.utcnow()
        db.session.commit()
        return True

    done = _set_and_commit()
    if done is None:
        return None
    if not done:
        raise PipRefused('Set it up first: a swapped copy needs both pictures.', 409)
    log.info('Picture-in-picture channel %d: swapped copy %s', row_id, 'offered' if on else 'no longer offered')
    return db.session.get(PipChannel, row_id)


# ---------------------------------------------------------------------------
# What a picture plays
# ---------------------------------------------------------------------------

@dataclass
class Pane:
    """What one picture is right now: its state (PANE_*), what it is called, and for
    PANE_LIVE the output_feed.Play to start. `start` and `stop` are a game's time, `listed_on`
    the channel a coming game is listed on, `why` a clause naming what stops it."""
    state: str
    source: Optional[Source] = None
    name: str = ''
    play: object = field(default=None, repr=False)
    event: object = field(default=None, repr=False)
    noun: str = 'game'
    start: Optional[datetime] = None
    stop: Optional[datetime] = None
    listed_on: str = ''
    why: str = ''


@dataclass
class Tuned:
    """A picture-in-picture channel as one tune-in finds it (output_feed.Play.pip): the row,
    which of its two feed ids was asked for, and its big picture's Pane, None with nothing set up."""
    row_id: int
    name: str
    swapped: bool
    set_up: bool
    pane: Optional[Pane] = None


def _row_play(feed_id: int, cfg: dict, exclude_ids, viewer_id) -> tuple:
    """(play, could) for a channel's or a group's own feed row, its On the output switch aside:
    `could` says a play with nothing excluded would start, which is what tells a row whose
    channels all dropped from one that cannot play at all."""
    play = output_feed.resolve_play(feed_id, cfg, exclude_ids=exclude_ids, viewer_id=viewer_id,
                                    any_switch=True)
    if play is None or play.member is not None or not exclude_ids:
        return play, play is not None and play.member is not None
    again = output_feed.resolve_play(feed_id, cfg, viewer_id=viewer_id, any_switch=True)
    return play, again is not None and again.member is not None


def _matches(words: str, rows) -> list:
    """The guide rows a search names: those whose name has the words in it, any case, in the
    order given (the TV Guide's)."""
    needle = words.casefold()
    return [r for r in rows if needle in (r.name or '').casefold()]


def resolve_source(source: Optional[Source], cfg: dict, *, feed_id: int, label: str,
                   now: Optional[datetime] = None, exclude_ids=frozenset(), event_id: int = None,
                   viewer_id=None) -> Pane:
    """What `source` plays right now, with the code the matching plain play uses (section 5). A
    game plays what a sports channel carrying it would; a channel or a group what its own feed
    row would, whether or not that row is on the output; a search the first guide row whose name
    has its words, on an account with a connection free when one is. `exclude_ids` are the
    channels that already failed this play, `event_id` the game a play is already on, which the
    clock never ends. `feed_id` and `label` are the play's own, carried onto a game's Play."""
    now = now or datetime.utcnow()
    if source is None:
        return Pane(PANE_EMPTY)
    if source.kind == PIP_SOURCE_GAME:
        return _resolve_game(source, cfg, feed_id, label, now, exclude_ids, event_id, viewer_id)
    if source.kind in (PIP_SOURCE_CHANNEL, PIP_SOURCE_GROUP):
        group = source.kind == PIP_SOURCE_GROUP
        what = 'channel group' if group else 'channel'
        if source.ref is None:
            # Unlinked by detach_refs() when it was deleted.
            return Pane(PANE_GONE, source, f'A {what} that was removed',
                        why=f'the {what} is no longer in your TV Guide')
        row = db.session.get(ChannelGroup if group else Channel, source.ref)
        row_feed = (output_feed.feed_id_for_group if group else output_feed.feed_id_for_channel)(source.ref)
        play, could = _row_play(row_feed, cfg, exclude_ids, viewer_id)
        if row is None or play is None:
            return Pane(PANE_GONE, source, row.name if row is not None else f'A {what} that was removed',
                        why=f'the {what} is no longer in your TV Guide')
        if play.member is not None:
            return Pane(PANE_LIVE, source, row.name, play=play)
        if could:
            return Pane(PANE_RECONNECTING, source, row.name, why=play.why_none)
        return Pane(PANE_CANNOT, source, row.name, why=play.why_none)
    if source.kind == PIP_SOURCE_SEARCH:
        name = f'Channels named "{source.query}"'
        rows = _matches(source.query, output_feed.guide_rows(output_feed.feed_config(cfg)))
        if not rows:
            return Pane(PANE_NO_MATCH, source, name, why='no channel matches its search')
        first = None
        could_any = False
        why = ''
        for row in rows[:SEARCH_WALK]:
            play, could = _row_play(row.feed_id, cfg, exclude_ids, viewer_id)
            could_any = could_any or could
            if play is None or play.member is None:
                why = why or (play.why_none if play is not None else '')
                continue
            standing = output_feed._viewer_standing({play.member.account_id}, viewer_id)
            if standing.get(play.member.account_id) == connlim.SEAT_FREE:
                return Pane(PANE_LIVE, source, name, play=play)
            first = first or play
        if first is not None:
            return Pane(PANE_LIVE, source, name, play=first)
        return Pane(PANE_RECONNECTING if could_any else PANE_CANNOT, source, name,
                    why=why or 'none of the channels it matches can play right now')
    raise ValueError(f'Unknown kind of picture "{source.kind}"')


def _resolve_game(source: Source, cfg: dict, feed_id: int, label: str, now: datetime, exclude_ids,
                  event_id, viewer_id) -> Pane:
    from . import sports_feed, sports_match
    from .sports_data import event_game_name
    from .sports_view import event_noun
    event = SportsEvent.query.filter_by(tsdb_id=source.ref).first()
    if event is None:
        return Pane(PANE_GONE, source, 'A game that was removed',
                    why='the game is no longer in the sports schedule')
    league = db.session.get(SportsLeague, event.league_id)
    name = f'{league.name}: {event_game_name(event)}'
    pane = Pane(PANE_LIVE, source, name, event=event, noun=event_noun(league, event))
    held = event_id is not None and event_id == event.id
    play = output_feed.resolve_game_play(event, league, feed_id, label, cfg, exclude_ids=exclude_ids,
                                         held=held, now=now, viewer_id=viewer_id)
    window = sports_feed.game_windows([event], now)[event.id]
    if window is not None:
        pane.start, pane.stop = window
    if play.member is not None:
        pane.play = play
        return pane
    if play.found.channels:
        # It is on, and no channel carrying it may be played.
        could = bool(exclude_ids) and output_feed.resolve_game_play(
            event, league, feed_id, label, cfg, held=held, now=now, viewer_id=viewer_id).member is not None
        pane.state, pane.why = (PANE_RECONNECTING if could else PANE_CANNOT), play.why_none
        return pane
    before, after = output_feed.sports_window(output_feed.feed_config(cfg))
    if window is None:
        pane.state = PANE_NOT_LISTED
    elif now < window[0] - before:
        pane.state = PANE_BEFORE
        coming = sorted((row[2], row[0]) for row in sports_match.listing_windows([event.id], now).get(event.id, ())
                        if row[2] is not None and row[2] > now)
        if coming:
            listed = db.session.get(Channel, coming[0][1])
            pane.listed_on = listed.name if listed is not None else ''
    elif now >= window[1] + after:
        pane.state = PANE_OVER
    else:
        pane.state = PANE_NOT_LISTED
    return pane


def resolve_feed(feed_id: int, cfg: dict, exclude_ids=frozenset(), event_id: int = None,
                 now: Optional[datetime] = None, viewer_id=None, preview: bool = False,
                 own_accounts: bool = False):
    """output_feed.resolve_play() for a picture-in-picture feed id: None for one that names
    nothing served (no such row, or a swapped copy that is not offered), else a Play carrying a
    Tuned. Its `member` is the channel the big picture plays, None when the play is on its card;
    never a fault, since a card is an ordinary state. A channel taken off the output is refused
    by name as any row is, and a preview plays it all the same. `own_accounts` is a play whose
    login plays only from accounts of its own, which is refused by name: the pictures are shared
    by everyone watching and come from the output's accounts (dev/changelog/1765)."""
    row_id = output_feed.decode_feed_id(feed_id)[1]
    swapped = output_feed.pip_swapped(feed_id)
    row = db.session.get(PipChannel, row_id)
    if row is None or (swapped and not (row.swapped_copy and is_set(row))):
        return None
    name = Served(row, swapped).name
    label = f'picture-in-picture channel {row.id} ({name})'
    if not row.on_output and not preview:
        return output_feed.Play(feed_id, label, why_none=output_feed.WHY_TAKEN_OFF, fault=False)
    if own_accounts:
        return output_feed.Play(feed_id, label, why_none=output_feed.WHY_PIP_OWN_ACCOUNTS, fault=False,
                                own_accounts=True)
    tuned = Tuned(row.id, name, swapped, is_set(row))
    play = output_feed.Play(feed_id, label, fault=False, preview=preview, pip=tuned)
    if not tuned.set_up:
        return play
    big, _small = pictures(row, swapped)
    pane = tuned.pane = resolve_source(big, cfg, feed_id=feed_id, label=label, now=now,
                                       exclude_ids=exclude_ids, event_id=event_id, viewer_id=viewer_id)
    if pane.state == PANE_LIVE:
        inner = pane.play
        play.member, play.group, play.event_id = inner.member, inner.group, inner.event_id
        play.picked, play.note = inner.picked, inner.note
        play.label = f'{label}: {pane.name}'
    return play


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------

@dataclass
class Card:
    """A channel's card: the sports_screen.Screen its clip is drawn from, the PANE_* it stands
    for, and what Watching now says of it."""
    screen: object = field(repr=False)
    state: str
    text: str


def _starts(start: datetime, now: datetime, tz, h24: bool) -> str:
    """'at 4:25 PM' today, 'tomorrow at 1:00 PM', 'Thu Nov 26 at 12:30 PM'."""
    day = day_label(start, now, tz)
    clock = format_local(start, 'clock', tz=tz, h24=h24)
    if day == 'Today':
        return f'at {clock}'
    return f'{day.lower() if day in ("Tomorrow", "Yesterday") else day} at {clock}'


def card_words(tuned: Tuned, cfg: dict, held: str = None, slots: int = 1,
               now: Optional[datetime] = None) -> tuple:
    """(state, headline, note, text): what the card of a channel says in its big window, and what
    Watching now says of it, a clause. `held` is a picture that could play and cannot start here
    yet (sports_screen.HELD_*)."""
    from . import sports_screen as ss
    now = now or datetime.utcnow()
    tz, h24 = get_display_tz(), is_24h()
    if not tuned.set_up:
        return (PANE_EMPTY, f'{tuned.name} has nothing on it',
                'Set up what it shows on ChannelBin\'s IPTV Output page, under Picture-in-picture channels.',
                'nothing is set up')
    pane = tuned.pane
    before, after = output_feed.sports_window(output_feed.feed_config(cfg))
    ahead = ss.before_words(int(before.total_seconds() // 60))
    if held == ss.HELD_NO_CONNECTION:
        return (PANE_BUSY, 'Waiting for a connection',
                f'Every account {pane.name} plays from is busy. It comes on by itself when one is free.',
                'waiting for a connection: every account it plays from is busy')
    if held == ss.HELD_NO_SLOT:
        return (PANE_BUSY, 'Waiting for a free place',
                f'{counted(slots, "IPTV player")} {"is" if slots == 1 else "are"} watching, which is the most '
                f'allowed at once. {pane.name} comes on by itself when one stops.',
                'waiting for one of the "IPTV players at once" slots')
    if held is not None:
        raise ValueError(f'Unknown reason a picture is held "{held}"')
    state = pane.state
    if state == PANE_BEFORE:
        on = f' on {pane.listed_on}' if pane.listed_on else ''
        listed = '' if pane.listed_on else ' No TV Guide lists it yet.'
        return (state, pane.name,
                f'Starts {_starts(pane.start, now, tz, h24)}{on}.{listed} It comes on here by itself {ahead} before.',
                f'starts {_starts(pane.start, now, tz, h24)}, no connection used until then')
    if state == PANE_NOT_LISTED:
        return (state, pane.name,
                f'No channel\'s guide lists this {pane.noun} yet. It comes on here by itself once one does.',
                'waiting for a guide to list it')
    if state == PANE_OVER:
        ended = (f' Its listing ended at {format_local(pane.stop, "clock", tz=tz, h24=h24)}.'
                 if pane.stop is not None else '')
        return (state, f'The {pane.noun} is over', f'{pane.name}.{ended}',
                f'its {pane.noun} is over, no connection used')
    if state == PANE_GONE:
        return (state, pane.name, f'{pane.why[:1].upper()}{pane.why[1:]}. Choose another picture on '
                'ChannelBin\'s IPTV Output page.', pane.why)
    if state == PANE_NO_MATCH:
        return (state, 'No channel matches',
                f'Nothing in your TV Guide is named "{pane.source.query}" right now. It comes on by itself '
                'when one is.', 'no channel matches its search')
    if state == PANE_CANNOT:
        return (state, f'{pane.name} cannot play right now',
                f'{pane.why[:1].upper()}{pane.why[1:]}.' if pane.why else '', pane.why or 'nothing can play it')
    if state == PANE_RECONNECTING:
        return (state, f'Reconnecting to {pane.name}',
                'Its stream dropped. Trying it again, and then any other channel that has it.',
                'dropped and is reconnecting')
    if state == PANE_TAKEN:
        return (state, 'A recording needs this connection',
                f'{pane.name} comes back by itself when a connection is free.',
                f'paused for a recording ({pane.why}); it comes back by itself' if pane.why
                else 'paused for a recording; it comes back by itself')
    if state == PANE_LIVE:
        raise ValueError('A picture that is playing has no card')
    if state in (PANE_EMPTY, PANE_SAME, PANE_YIELDED, PANE_BUSY, PANE_NO_GPU):
        # None of these is a big picture's own state: an unset picture is a channel not set
        # up, the busy card is `held`, and the rest belong to a small picture.
        raise ValueError(f'The big picture cannot be "{state}" here')
    raise ValueError(f'Unknown pane state "{state}"')


def pane_words(pane: Pane, small: bool = True, now: Optional[datetime] = None) -> tuple:
    """(what the small window's card says, what Watching now says of the picture, a clause)
    for a picture inside a combined play (section 11's table, the short forms). ('', '') for
    one that is playing. `small` is the only form there is: the big window's card is the
    channel's own (card_for)."""
    if not small:
        raise ValueError('The big window shows the channel\'s card, card_for()')
    state = pane.state
    if state == PANE_LIVE:
        return '', ''
    if state == PANE_EMPTY:
        return 'Nothing chosen', 'nothing is set up'
    if state == PANE_CANNOT:
        return 'Cannot play right now', pane.why or 'nothing can play it'
    if state == PANE_BEFORE:
        when = _starts(pane.start, now or datetime.utcnow(), get_display_tz(), is_24h())
        return f'Starts {when}', f'starts {when}, no connection used until then'
    if state == PANE_NOT_LISTED:
        return 'Not in the guide yet', 'waiting for a guide to list it'
    if state == PANE_OVER:
        return f'The {pane.noun} is over', f'its {pane.noun} is over, no connection used'
    if state == PANE_GONE:
        return 'No longer available', pane.why or 'it is no longer available'
    if state == PANE_NO_MATCH:
        return 'No channel matches', 'no channel matches its search'
    if state == PANE_SAME:
        return 'Same channel as the big picture', 'would be the same channel as the big picture'
    if state == PANE_RECONNECTING:
        return 'Reconnecting', 'dropped and is reconnecting'
    if state == PANE_TAKEN:
        return 'Paused for a recording', (f'paused for a recording ({pane.why}); it comes back by itself'
                                          if pane.why else 'paused for a recording; it comes back by itself')
    if state == PANE_YIELDED:
        return ('Paused: the big picture needed its connection',
                'paused: the big picture needed its connection')
    if state == PANE_BUSY:
        return 'Waiting for a connection', 'waiting for a connection: every account it plays from is busy'
    if state == PANE_NO_GPU:
        return '', 'off: nothing here can combine two pictures'
    raise ValueError(f'Unknown pane state "{state}"')


def card_for(tuned: Tuned, cfg: dict, held: str = None, slots: int = 1) -> Card:
    """The card a channel shows in place of a picture: the sports screens' own look (their fonts,
    their hero, ChannelBin's logo and the channel's name), with words of its own."""
    from . import sports_screen as ss
    state, headline, note, text = card_words(tuned, cfg, held, slots)
    screen = ss.Screen(channel=tuned.name, color=ss.ACCENT, state=ss.STATE_CARD, mode=ss.MODE_PROXY,
                       words=ss.Words(kicker='Picture in picture', headline=headline, when='', note=note))
    return Card(screen, state, text)


# ---------------------------------------------------------------------------
# What a page and the guide say of a channel
# ---------------------------------------------------------------------------

@dataclass
class Described:
    """One picture in words, for a page or the guide, with no play asked: `title` its full name,
    `short` what names it in a small space, `sub` when it is on and where. `gone` is a game or
    a channel that no longer exists. `window` is a game's (start, stop), `member` the channel a
    play of a channel, a group or a search would start from when the caller had the guide's rows."""
    kind: str
    title: str
    short: str
    sub: str = ''
    gone: bool = False
    over: bool = False
    window: Optional[tuple] = None
    member: object = field(default=None, repr=False)
    matches: int = 0


def describe(rows, now: Optional[datetime] = None, guide=None, after: Optional[timedelta] = None) -> dict:
    """{(row id, role): Described} for every picture of `rows` (PipChannel), in a fixed number of
    queries whatever their number: the games, their leagues, their listings, the channels and the
    groups named. `guide` is output_feed.guide_rows() when the caller already holds it (the IPTV
    Output page), which also names the channel each picture would start from; without it a
    search is not counted. `after` is how long past its end a game still counts as on."""
    from . import sports_feed, sports_match
    from .sports_data import event_game_name
    now = now or datetime.utcnow()
    after = after if after is not None else timedelta(0)
    tz, h24 = get_display_tz(), is_24h()
    sources = {(row.id, role): source_of(row, role) for row in rows for role in ROLES}
    sources = {k: s for k, s in sources.items() if s is not None}

    def refs(kind):
        return sorted({s.ref for s in sources.values() if s.kind == kind and s.ref is not None})

    events = ({e.tsdb_id: e for e in SportsEvent.query.filter(SportsEvent.tsdb_id.in_(refs(PIP_SOURCE_GAME)))}
              if refs(PIP_SOURCE_GAME) else {})
    leagues = ({lg.id: lg for lg in SportsLeague.query.filter(
        SportsLeague.id.in_({e.league_id for e in events.values()}))} if events else {})
    windows = sports_feed.game_windows(list(events.values()), now) if events else {}
    listings = (sports_match.listing_windows([e.id for e in events.values()], now) if events else {})
    by_feed = {r.feed_id: r for r in guide} if guide is not None else None
    listed_on = {event_id: sorted(found, key=lambda r: (r[2] is None, r[2]))[0][0]
                 for event_id, found in listings.items() if found}
    channel_ids = sorted(set(refs(PIP_SOURCE_CHANNEL)) | set(listed_on.values()))
    channels = ({c.id: c for c in Channel.query.filter(Channel.id.in_(channel_ids))} if channel_ids else {})
    groups = ({g.id: g for g in ChannelGroup.query.filter(ChannelGroup.id.in_(refs(PIP_SOURCE_GROUP)))}
              if refs(PIP_SOURCE_GROUP) else {})
    sizes = (dict(db.session.query(ChannelGroupMember.group_id, db.func.count(ChannelGroupMember.id))
                  .filter(ChannelGroupMember.group_id.in_(list(groups)))
                  .group_by(ChannelGroupMember.group_id).all()) if groups else {})
    out = {}
    for key, source in sources.items():
        if source.kind == PIP_SOURCE_GAME:
            event = events.get(source.ref)
            if event is None:
                out[key] = Described(source.kind, 'A game that is no longer in the sports schedule',
                                     'Game removed', gone=True)
                continue
            game = event_game_name(event)
            window = windows.get(event.id)
            on = channels.get(listed_on.get(event.id))
            where = f', on {on.name}' if on is not None else '. Not in your TV Guide yet'
            over = window is not None and now >= window[1] + after
            if window is None:
                when = 'Its time is not known yet'
            elif over:
                when = f'Ended {_starts(window[1], now, tz, h24)}'
            elif now >= window[0]:
                when = f'On now, since {format_local(window[0], "clock", tz=tz, h24=h24)}'
            else:
                when = f'Starts {_starts(window[0], now, tz, h24)}'
            out[key] = Described(source.kind, f'{leagues[event.league_id].name}: {game}', game,
                                 f'{when}{where}', over=over, window=window)
        elif source.kind == PIP_SOURCE_CHANNEL:
            ch = channels.get(source.ref)
            if ch is None or not ch.in_guide:  # guide-flag-read-ok: a picture is a channel that is its own guide row
                out[key] = Described(source.kind, 'A channel that is no longer in your TV Guide',
                                     ch.name if ch is not None else 'Channel removed', gone=True)
                continue
            row = by_feed.get(output_feed.feed_id_for_channel(ch.id)) if by_feed is not None else None
            out[key] = Described(source.kind, ch.name, ch.name, 'Channel',
                                 member=row.plays if row is not None else None)
        elif source.kind == PIP_SOURCE_GROUP:
            group = groups.get(source.ref)
            if group is None or not group.in_guide:
                out[key] = Described(source.kind, 'A channel group that is no longer in your TV Guide',
                                     group.name if group is not None else 'Group removed', gone=True)
                continue
            row = by_feed.get(output_feed.feed_id_for_group(group.id)) if by_feed is not None else None
            out[key] = Described(source.kind, group.name, group.name,
                                 f'Channel group, {counted(sizes.get(group.id, 0), "channel")}',
                                 member=row.plays if row is not None else None)
        elif source.kind == PIP_SOURCE_SEARCH:
            title, short = f'Channels named "{source.query}"', f'"{source.query}"'
            if guide is None:
                out[key] = Described(source.kind, title, short, 'A search')
                continue
            hits = _matches(source.query, guide)
            member = next((r.plays for r in hits if r.plays is not None), None)
            out[key] = Described(source.kind, title, short,
                                 f'A search: {counted(len(hits), "channel")} {"matches" if len(hits) == 1 else "match"} now'
                                 if hits else 'A search: nothing matches right now',
                                 member=member, matches=len(hits))
        else:
            raise ValueError(f'Unknown kind of picture "{source.kind}"')
    return out


def layout_words(row: PipChannel) -> str:
    if row.layout == PIP_LAYOUT_SIDE:
        return 'Side by side'
    if row.layout == PIP_LAYOUT_CORNER:
        return f'Small picture {CORNER_WORDS[row.corner].lower()}, {SIZE_WORDS[row.size].lower()}'
    raise ValueError(f'Unknown layout "{row.layout}"')


NOTHING_SET_UP = 'Nothing set up yet'


def guide_blocks(one: Served, described: dict, now: datetime, days: int, block: timedelta):
    """(start, stop, title, description) for the guide a player is sent (section 4.4): blocks of
    `block` on the clock's even hours across the window, titled from the two pictures, the big
    one first. Where both pictures are games whose times overlap or sit less than a block apart,
    the blocks they cover are one program from the earlier start to the later end, so a player's
    guide shows the two games as one show. No query: `described` is describe()'s."""
    row = one.row
    roles = (ROLE_INSET, ROLE_MAIN) if one.swapped else ROLES
    big, small = (described.get((row.id, role)) for role in roles)
    if big is None or small is None:
        title = NOTHING_SET_UP
        desc = ('A picture-in-picture channel with nothing set up yet. Choose what it shows on '
                'ChannelBin\'s IPTV Output page.')
        merged = None
    else:
        title = f'{big.short} + {small.short}'
        desc = (f'Two pictures at once: {big.title} as the big picture and {small.title} as the small one. '
                'Set up on ChannelBin\'s IPTV Output page.')
        merged = None
        if big.window is not None and small.window is not None:
            first, second = sorted((big.window, small.window))
            if second[0] - first[1] < block:
                merged = (first[0], max(first[1], second[1]))
    start = now.replace(minute=0, second=0, microsecond=0)
    start -= timedelta(hours=start.hour % 2)
    end = now + timedelta(days=days)
    if merged is not None and (merged[1] <= start or merged[0] >= end):
        merged = None
    while start < end:
        stop = start + block
        if merged is not None and stop > merged[0] and start < merged[1]:
            if start < merged[0]:
                yield start, merged[0], title, desc
            yield merged[0], merged[1], title, desc
            # The blocks go on from the even hour the games end inside, cut to start at their end.
            start = merged[1].replace(minute=0, second=0, microsecond=0)
            start -= timedelta(hours=start.hour % 2)
            stop = start + block
            if stop > merged[1]:
                yield merged[1], stop, title, desc
            start = stop
            merged = None
            continue
        yield start, stop, title, desc
        start = stop


# ---------------------------------------------------------------------------
# The setup dialog's "would play from" lines
# ---------------------------------------------------------------------------

#: How many of the small picture's channels are looked at for one off the big picture's account.
WOULD_PLAY_WALK = 5


def clean_draft_source(raw) -> Optional[Source]:
    """One picture of a draft the setup dialog is still editing: a Source, or None for one not
    chosen yet or not complete."""
    try:
        return _clean_source(raw, 'draft')
    except PipRefused:
        return None


def would_play(main: Optional[Source], inset: Optional[Source], cfg: dict) -> dict:
    """What a draft's two pictures would start from if a TV tuned in now, and what that costs
    in connections: {'main', 'inset' (each {'channel', 'account'} or None), 'seats' (a
    sentence)}. Each picture is asked as a play of it is (resolve_source), the big one first.
    Nothing is seated, so it is a look and not a promise. Requires an app context."""
    from .database import Account
    feed_id, label = 0, 'a picture-in-picture channel being set up'
    default = (cfg.get('accounts') or {}).get('default_max_connections', 1)
    allowed = output_feed.allowed_account_ids(output_feed.feed_config(cfg))
    accounts = [a for a in Account.query.all() if allowed is None or a.id in allowed]
    limits = connlim.limits_for_accounts(accounts, default)

    def member_of(source, exclude=frozenset()):
        if source is None:
            return None
        pane = resolve_source(source, cfg, feed_id=feed_id, label=label, exclude_ids=exclude)
        if pane.state != PANE_LIVE:
            return None
        member = pane.play.member
        # A channel on a busy account is where it would wait, not where it would play from.
        free = output_feed._viewer_standing({member.account_id}).get(member.account_id) == connlim.SEAT_FREE
        return member if free else None

    big = member_of(main)
    exclude = frozenset({big.id}) if big is not None else frozenset()
    def shares_one_seat(member) -> bool:
        return (big is not None and member.account_id == big.account_id
                and limits.get(big.account_id, 1) <= 1)

    # A play seats the big picture first, so the small one cannot share an account that has
    # one connection: it would start from its next channel down, or wait.
    small = member_of(inset, exclude) if inset != main else None
    for _ in range(WOULD_PLAY_WALK):
        if small is None or not shares_one_seat(small):
            break
        exclude = exclude | {small.id}
        small = member_of(inset, exclude)
    if small is not None and shares_one_seat(small):
        small = None
    names = {a.id: a.name for a in accounts}

    def said(member):
        return None if member is None else {'channel': member.name,
                                            'account': names.get(member.account_id) or member.account.name}

    live = [f'{m.name} on {names.get(m.account_id) or m.account.name}' for m in (big, small) if m is not None]
    total = sum(limits.values())
    held = connlim.holder_counts()
    free = max(0, total - sum(n for account_id, n in held.items() if account_id in limits))
    if main is None or inset is None:
        seats = ''
    else:
        if len(live) == 2:
            uses = f'While a TV watches it, it uses two connections: {live[0]} and {live[1]}.'
        elif len(live) == 1:
            uses = (f'While a TV watches it, it uses one connection now ({live[0]}), and a second when '
                    'the other picture comes on.')
        else:
            uses = 'Neither picture is on yet, so it uses no connection until one comes on.'
        seats = f'{uses} Right now {free} of your {counted(total, "connection")} {"is" if free == 1 else "are"} free.'
    return {'main': said(big), 'inset': said(small), 'seats': seats}


# ---------------------------------------------------------------------------
# The setup dialog's lists
# ---------------------------------------------------------------------------

def picker_data(cfg: dict, now: Optional[datetime] = None) -> dict:
    """What the setup dialog's pickers list: every coming game in the leagues followed, as far
    ahead as their schedules go (at most PICKER_GAMES, soonest first), and the TV Guide's own
    rows in its order, which is also what a search is matched against. A fixed number of queries."""
    from . import sports_feed, sports_match
    from .sports_data import event_game_name
    from .sports_view import event_noun
    now = now or datetime.utcnow()
    tz, h24 = get_display_tz(), is_24h()
    fcfg = output_feed.feed_config(cfg)
    before, after = output_feed.sports_window(fcfg)
    events = sports_feed.followed_coming(now, after, PICKER_GAMES * 2)
    games = []
    if events:
        leagues = {lg.id: lg for lg in SportsLeague.query.filter(
            SportsLeague.id.in_({e.league_id for e in events}))}
        windows = sports_feed.game_windows(events, now)
        listings = sports_match.listing_windows([e.id for e in events], now)
        first = {event_id: sorted(rows, key=lambda r: (r[2] is None, r[2]))[0][0]
                 for event_id, rows in listings.items() if rows}
        names = (dict(db.session.query(Channel.id, Channel.name).filter(Channel.id.in_(set(first.values()))).all())
                 if first else {})
        today = to_local(now, tz).date()
        for event in events:
            window = windows.get(event.id)
            league = leagues.get(event.league_id)
            if window is None or league is None or now >= window[1] + after:
                continue
            live = now >= window[0] - before
            on = names.get(first.get(event.id), '')
            clock = format_local(window[0], 'clock', tz=tz, h24=h24)
            when = f'On now, since {clock}' if now >= window[0] else clock
            games.append({
                'start': window[0], 'ref': event.tsdb_id, 'league': league.name, 'title': event_game_name(event),
                'noun': event_noun(league, event), 'live': live,
                'days': (to_local(window[0], tz).date() - today).days,
                'day': 'On now' if live else _day_heading(window[0], now, tz),
                'sub': f'{league.name}. {when}{f", on {on}" if on else ". Not in your TV Guide yet"}',
                'label_sub': (f'{"On now, since " + clock if now >= window[0] else "Starts " + _starts(window[0], now, tz, h24)}'
                              f'{f", on {on}" if on else ". Not in your TV Guide yet"}'),
            })
        games.sort(key=lambda g: (not g['live'], g['start']))
        games = [{k: v for k, v in g.items() if k != 'start'} for g in games[:PICKER_GAMES]]
    accounts = dict(db.session.query(Account.id, Account.name).all())
    rows = []
    for row in output_feed.guide_rows(fcfg):
        if row.kind == output_feed.KIND_GROUP:
            sub = f'Channel group, {counted(len(row.row.memberships), "channel")}'
        elif row.kind == output_feed.KIND_CHANNEL:
            sub = f'Channel on {accounts.get(row.row.account_id, "an account that was removed")}'
        else:
            raise ValueError(f'Not a TV Guide row: "{row.kind}"')
        rows.append({'kind': row.kind, 'ref': row.row.id, 'name': row.name, 'sub': sub})
    return {'games': games, 'channels': rows, 'guide_days': output_feed.guide_days(fcfg),
            'name_max': NAME_MAX, 'query_max': QUERY_MAX}


def _day_heading(when: datetime, now: datetime, tz) -> str:
    """'Today', 'Tomorrow', else 'Thursday, Nov 26': a day's heading in the games picker."""
    day = day_label(when, now, tz)
    return day if day in ('Today', 'Tomorrow', 'Yesterday') else format_local(when, 'weekday_monthday', tz=tz)
