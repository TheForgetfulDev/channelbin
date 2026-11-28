"""The IPTV output's arrangement: its categories, with ids that stay put, and where each channel
sits in them (dev/docs/DESIGN-output-feed.md 16, dev/changelog/1797).

A category is an `output_categories` row and its id is the `category_id` an IPTV player is sent.
An item is what moves as one piece: a feed row, or all of a league's numbered sports channels
(`item_key()`). These functions, and nothing else, write these tables:

- `reconcile()` gives a new item or category its row, at the end of the category or list it
  arrives in, marked `placed_auto`, and deletes an item whose row is gone. It runs after every
  write request, after the sports placement pass, after a sync and at startup.
- `arrange()` applies what is stored to catalog()'s rows. It never writes: the list is built on
  a player's GET.
- `forget_items()` deletes an item inside the unit that deletes the row it names, since channel,
  group and league ids are reused.
- `carry_item()` hands a channel's place to the channel its guide row moved to.
- `backfill()` discharges migration 159 once, from today's order.

An item's category is fixed when it is placed. A provider renaming or dropping a category moves
nothing (16.2)."""

import logging
from datetime import datetime, timedelta

from sqlalchemy.exc import OperationalError, SQLAlchemyError

from . import db
from .database import (OUTPUT_BUILTIN_CHANNELBIN, OUTPUT_BUILTIN_UNCATEGORIZED,
                       OUTPUT_CATEGORY_ARRIVED, OUTPUT_ITEM_FEED, OUTPUT_ITEM_LEAGUE,
                       OutputCategory, OutputItem)
from .db_utils import retry_on_locked
from .fmt_utils import counted

log = logging.getLogger(__name__)

#: How often reconcile() moves a category's `last_sent_at`: the page shows a date, so a write on
#: every run would buy nothing and make every web UI write a second one.
LAST_SENT_STEP = timedelta(hours=1)


def source_key(name) -> str:
    """A category's identity: the name it arrives with, stripped, inner whitespace collapsed and
    case-folded. The one spelling, at write and at lookup."""
    return ' '.join((name or '').split()).casefold()


def _builtin_of(key: str):
    from .output_feed import UNCATEGORIZED, UP_NEXT_CATEGORY
    if key == source_key(UNCATEGORIZED):
        return OUTPUT_BUILTIN_UNCATEGORIZED
    if key == source_key(UP_NEXT_CATEGORY):
        return OUTPUT_BUILTIN_CHANNELBIN
    return None


def item_key(row) -> tuple:
    """(item_kind, item_id) of a catalog FeedRow: a league's numbered sports channel is its
    league's item, everything else its own feed row's (a picture-in-picture channel's swapped
    copy its channel's, as in a lineup)."""
    from . import output_feed as of
    if row.kind == of.KIND_SPORTS and not row.row.is_team:
        return OUTPUT_ITEM_LEAGUE, row.row.league.id
    if row.kind in (of.KIND_CHANNEL, of.KIND_GROUP, of.KIND_SPORTS, of.KIND_UP_NEXT, of.KIND_PIP):
        return OUTPUT_ITEM_FEED, of.lineup_key(row.feed_id)
    raise ValueError(f'unknown feed row kind {row.kind!r}')


def category_name(cat) -> str:
    """What a category is sent as: the owner's name, else the one it arrived with."""
    return cat.name or cat.source_name or ''


# ── What the output could list ─────────────────────────────────────────────────────────────

def _arrived_as(category_name_) -> str:
    from .output_feed import UNCATEGORIZED
    return (category_name_ or '').strip() or UNCATEGORIZED


def _arrivals(known) -> list:
    """[(item key, the category it arrives in)] for every item the output could list, in the
    catalog's order. Reads identities and names only - no health, no games, no ORM objects for
    the guide's rows - so it is cheap enough to run after every write. A group's category is
    read only when it has no item in `known` (None for one that has), since nothing else needs
    it: a group with no pinned listings member arrives in its first member's category, where
    the catalog would ask which member plays now, and the answer is used once, when the group is
    placed. Eight queries, nine with a new group."""
    # duplicated from output_feed.guide_rows and _own_rows - the same rows in the same order,
    # without the health and game reads those need for a play
    from . import output_feed as of
    from . import sports_feed, sports_up_next
    from .channel_groups import guide_row_sort_key
    from .database import Channel, ChannelGroup, ChannelGroupMember, PipChannel
    guide = []
    for ch in (db.session.query(Channel.id, Channel.name, Channel.category_name, Channel.guide_sort_order)
               .filter(Channel.in_guide.is_(True))):  # guide-flag-read-ok: the feed's own-row half, as guide_rows()
        guide.append((guide_row_sort_key(ch), (OUTPUT_ITEM_FEED, of.feed_id_for_channel(ch.id)),
                      _arrived_as(ch.category_name)))
    groups = (db.session.query(ChannelGroup.id, ChannelGroup.name, ChannelGroup.guide_sort_order,
                               ChannelGroup.guide_listings_channel_id)
              .filter(ChannelGroup.in_guide.is_(True)).all())
    new = {g.id for g in groups if (OUTPUT_ITEM_FEED, of.feed_id_for_group(g.id)) not in known}
    members = {}
    if new:
        for group_id, channel_id, category in (
                db.session.query(ChannelGroupMember.group_id, ChannelGroupMember.channel_id, Channel.category_name)
                .join(Channel, Channel.id == ChannelGroupMember.channel_id)
                .filter(ChannelGroupMember.group_id.in_(new))
                .order_by(ChannelGroupMember.group_id, ChannelGroupMember.position)):
            members.setdefault(group_id, []).append((channel_id, category))
    for g in groups:
        name = None
        if g.id in new:
            # channel_groups.guide_listings_member(): a pin on a channel that left is ignored.
            listed = members.get(g.id, [])
            lead = next((c for i, c in listed if i == g.guide_listings_channel_id),
                        listed[0][1] if listed else '')
            name = _arrived_as(lead)
        guide.append((guide_row_sort_key(g), (OUTPUT_ITEM_FEED, of.feed_id_for_group(g.id)), name))
    guide.sort(key=lambda e: e[0])
    out = [(key, name) for _order, key, name in guide]
    by_league = {}
    for ch in sports_up_next.channels(everything=True):
        key = (OUTPUT_ITEM_FEED, of.feed_id_for_up_next(ch.number))
        if ch.everything:
            out.append((key, of.UP_NEXT_CATEGORY))
        else:
            by_league.setdefault(ch.league.id, (ch.league, []))[1].append((key, ch.league.name))
    for one in sports_feed.served_channels():
        key = ((OUTPUT_ITEM_FEED, of.feed_id_for_sports(one.row.id)) if one.is_team
               else (OUTPUT_ITEM_LEAGUE, one.league.id))
        by_league.setdefault(one.league.id, (one.league, []))[1].append((key, one.league.name))
    for _id, (_league, items) in sorted(by_league.items(), key=lambda kv: (kv[1][0].name.lower(), kv[0])):
        out += items
    for (pip_id,) in db.session.query(PipChannel.id).order_by(PipChannel.id):
        out.append(((OUTPUT_ITEM_FEED, of.feed_id_for_pip(pip_id)), of.UP_NEXT_CATEGORY))
    return out


# ── Placing ────────────────────────────────────────────────────────────────────────────────

class _Placer:
    """The stored categories and items, loaded once, and the one way a new one is added: at the
    end of the list, or of its category."""

    def __init__(self, now: datetime):
        self.now = now
        cats = OutputCategory.query.all()
        # source_key is UNIQUE, and NULL only on a made category, which nothing arrives in.
        self.by_key = {c.source_key: c for c in cats if c.source_key is not None}
        # (item_kind, item_id) is UNIQUE.
        self.items = {(i.item_kind, i.item_id): i for i in OutputItem.query.all()}
        self.cat_end = max((c.position for c in cats), default=-1) + 1
        self.item_end = {}
        for item in self.items.values():
            self.item_end[item.category_id] = max(self.item_end.get(item.category_id, 0), item.position + 1)
        self.stamped = set()
        self.added = 0

    def category(self, source_name: str, auto: bool):
        key = source_key(source_name)
        cat = self.by_key.get(key)
        if cat is None:
            cat = OutputCategory(origin=OUTPUT_CATEGORY_ARRIVED, builtin=_builtin_of(key),
                                 source_key=key, source_name=source_name.strip(),
                                 position=self.cat_end, placed_auto=auto, last_sent_at=self.now,
                                 created_at=self.now)
            db.session.add(cat)
            db.session.flush()
            self.by_key[key] = cat
            self.cat_end += 1
            self.added += 1
        return cat

    def stamp(self, cat) -> None:
        if cat.id in self.stamped:
            return
        self.stamped.add(cat.id)
        if cat.last_sent_at is None or cat.last_sent_at < self.now - LAST_SENT_STEP:
            cat.last_sent_at = self.now
            self.added += 1

    def item(self, key: tuple, source_name, auto: bool) -> None:
        """Place `key` in the category `source_name` names, unless it has a place. For one that
        has, only stamp that category when it exists: an item's category is fixed once placed,
        so a name it arrives under now makes no row of its own (16.2). `source_name` is None
        when the caller did not read it (an existing group's)."""
        if key in self.items:
            cat = self.by_key.get(source_key(source_name)) if source_name is not None else None
            if cat is not None:
                self.stamp(cat)
            return
        cat = self.category(source_name, auto)
        self.stamp(cat)
        position = self.item_end.get(cat.id, 0)
        item = OutputItem(item_kind=key[0], item_id=key[1], category_id=cat.id,
                          home_category_id=cat.id, position=position, placed_auto=auto,
                          created_at=self.now)
        db.session.add(item)
        self.items[key] = item
        self.item_end[cat.id] = position + 1
        self.added += 1


def _gone_items(items: dict) -> list:
    """The stored items whose channel, group, league or ChannelBin channel no longer exists. One
    query over every kind that has items. The Up next channel for everything has no row of its
    own and is never gone."""
    from sqlalchemy import literal, select, union_all
    from . import output_feed as of
    from .database import (Channel, ChannelGroup, PipChannel, SportsFeedChannel, SportsLeague,
                           SportsUpNextChannel)
    by_table = {Channel: {}, ChannelGroup: {}, SportsFeedChannel: {}, SportsUpNextChannel: {},
                PipChannel: {}, SportsLeague: {}}
    table_of = {of.KIND_CHANNEL: Channel, of.KIND_GROUP: ChannelGroup, of.KIND_SPORTS: SportsFeedChannel,
                of.KIND_UP_NEXT: SportsUpNextChannel, of.KIND_PIP: PipChannel}
    for key, item in items.items():
        if key[0] == OUTPUT_ITEM_LEAGUE:
            by_table[SportsLeague].setdefault(key[1], []).append(item)
            continue
        kind, row_id = of.decode_feed_id(key[1])
        if kind == of.KIND_UP_NEXT and row_id == 0:
            continue
        by_table[table_of[kind]].setdefault(row_id, []).append(item)
    asked = [(table.__tablename__, table, wanted) for table, wanted in by_table.items() if wanted]
    if not asked:
        return []
    have = set(db.session.execute(union_all(*[
        select(literal(name).label('t'), table.id).where(table.id.in_(list(wanted)))
        for name, table, wanted in asked])).all())
    return [item for name, _table, wanted in asked for row_id, found in wanted.items()
            if (name, row_id) not in have for item in found]


def reconcile(now: datetime = None) -> int:
    """Record every item and category the output could list that has no row yet, stamp the
    categories something arrives in, and delete the items whose row is gone (16.4). How many
    rows it wrote; usually none, and then it commits nothing. Drops the kept catalog answers
    after a commit that changed anything. Takes no admission ticket: tens of rows."""
    @retry_on_locked()
    def _reconcile_and_commit():
        placer = _Placer(now or datetime.utcnow())
        gone = _gone_items(placer.items)
        for item in gone:
            del placer.items[(item.item_kind, item.item_id)]
            db.session.delete(item)
        for key, source_name in _arrivals(placer.items):
            placer.item(key, source_name, auto=True)
        written = placer.added + len(gone)
        if written:
            db.session.commit()
        else:
            db.session.rollback()
        return written, len(gone)

    written, gone = _reconcile_and_commit()
    if written:
        from .output_feed import forget_answers
        forget_answers()
        if gone:
            log.info('IPTV output: forgot the place of %s whose row is gone', counted(gone, 'channel'))
    return written


def reconcile_guarded(where: str) -> int:
    """reconcile() for a caller whose own work must not fail with it: a write request already
    answered, a sync, the sports placement pass. A database error is logged with its traceback
    and the next run records what this one missed; until then a list built meanwhile places the
    item itself and says so (arrange())."""
    try:
        return reconcile()
    except SQLAlchemyError:
        db.session.rollback()
        log.warning('IPTV output: recording new channels\' places after %s failed; the next write '
                    'tries again', where, exc_info=True)
        return 0


def backfill(cfg) -> None:
    """Discharge migration 159's obligation: record today's catalog as the arrangement, in
    today's order - categories in the order their first channel comes, each one's items in
    order - with nothing marked placed automatically, and stamp the obligation in the same
    commit. Run from create_app() before the scheduler starts. Adds only what has no row, so a
    second run after a crash before the stamp changes nothing it already wrote."""
    from .migrations import _BF_OUTPUT_ARRANGEMENT, finish_obligation, obligation_pending
    from .output_feed import unarranged_rows
    if not obligation_pending(_BF_OUTPUT_ARRANGEMENT):
        return

    @retry_on_locked()
    def _fill_and_commit():
        placer = _Placer(datetime.utcnow())
        for row in unarranged_rows(cfg):
            placer.item(item_key(row), row.category, auto=False)
        finish_obligation(_BF_OUTPUT_ARRANGEMENT)
        db.session.commit()
        return len(placer.by_key), len(placer.items)

    cats, items = _fill_and_commit()
    log.info('IPTV output: recorded today\'s order as %s in %s',
             counted(items, 'item'), counted(cats, 'category', 'categories'))


# ── Deleting ───────────────────────────────────────────────────────────────────────────────

def forget_items(feed_ids=(), league_ids=()) -> None:
    """Delete the items of these feed rows and leagues, and take the feed ids out of every
    login's channel list (output_feed.forget_lineup_ids()). Called inside the unit that deletes
    the rows, by every path that deletes a channel, a group, a league or a picture-in-picture
    channel: their ids can be handed out again, and an item left behind would give the next row
    with that id this one's place. Mutates without committing; reconcile() deletes any it still
    finds as a second net."""
    from .output_feed import forget_lineup_ids, lineup_key
    keys = sorted({lineup_key(f) for f in feed_ids})
    if keys:
        OutputItem.query.filter(OutputItem.item_kind == OUTPUT_ITEM_FEED,
                                OutputItem.item_id.in_(keys)).delete(synchronize_session=False)
        forget_lineup_ids(keys)
    leagues = sorted(set(league_ids))
    if leagues:
        OutputItem.query.filter(OutputItem.item_kind == OUTPUT_ITEM_LEAGUE,
                                OutputItem.item_id.in_(leagues)).delete(synchronize_session=False)


def carry_item(old_feed_id: int, new_feed_id: int) -> None:
    """A channel's guide row moved to another channel (a Re-point, a duplicate removed with
    transfer): the new one takes the old one's place on the output, so its number and folder
    stay what the player had. When the new one already has a place, the owner arranged it, so
    that one is kept and the old one's is deleted (DESIGN-output-feed.md 16.1). Mutates without
    committing: called inside the move's own unit."""
    from .output_feed import lineup_key
    old_key, new_key = lineup_key(old_feed_id), lineup_key(new_feed_id)
    old = OutputItem.query.filter_by(item_kind=OUTPUT_ITEM_FEED, item_id=old_key).first()
    if old is None:
        return
    if OutputItem.query.filter_by(item_kind=OUTPUT_ITEM_FEED, item_id=new_key).first() is not None:
        db.session.delete(old)
    else:
        old.item_id = new_key


# ── Applying it ────────────────────────────────────────────────────────────────────────────

def _next_category_id(cats: dict) -> int:
    """The id SQLite will give the next category: one past the highest it ever issued, which
    sqlite_sequence holds for an AUTOINCREMENT table."""
    from sqlalchemy import text
    try:
        seq = db.session.execute(text("SELECT seq FROM sqlite_sequence WHERE name = 'output_categories'")).scalar()
    except OperationalError:
        seq = None
    return max(seq or 0, max(cats, default=0)) + 1


def arrange(rows: list) -> list:
    """catalog()'s rows, unnumbered, in the stored arrangement (16.5): each row's `category` and
    `category_id` are its item's category's, categories in their order, items in theirs, and a
    row keeps its place inside its item (a league's numbered channels in number order, a
    picture-in-picture channel's swapped copy after it). Two queries, none per row.

    It never writes. A row with no item is a write path that missed reconcile(): it goes at the
    end of the category it arrives in, and a category with no row goes at the end of the list
    under its arriving name, with the id the next reconcile() will most likely give it. The
    answer is not kept (output_feed.answer_not_kept()) and a WARNING names each row, so the
    next ask, after any write, comes out recorded."""
    from .output_feed import answer_not_kept
    cats = {c.id: c for c in OutputCategory.query.all()}
    items = {(i.item_kind, i.item_id): i for i in OutputItem.query.all()}
    by_key = {c.source_key: c for c in cats.values() if c.source_key is not None}
    cat_end = max((c.position for c in cats.values()), default=-1) + 1
    item_end = {}
    for item in items.values():
        item_end[item.category_id] = max(item_end.get(item.category_id, 0), item.position + 1)
    provisional = {}
    next_id = None
    spots = {}
    unrecorded = []
    keyed = []
    for index, row in enumerate(rows):
        key = item_key(row)
        spot = spots.get(key)
        if spot is None:
            item = items.get(key)
            cat = cats.get(item.category_id) if item is not None else None
            if cat is not None:
                spot = (cat.position, cat.id, item.position, item.id, category_name(cat))
            else:
                unrecorded.append(f'{row.name} ({row.feed_id})')
                arrived = _arrived_as(row.category)
                cat = by_key.get(source_key(arrived))
                if cat is not None:
                    cat_pos, cat_id, name = cat.position, cat.id, category_name(cat)
                else:
                    if source_key(arrived) not in provisional:
                        if next_id is None:
                            next_id = _next_category_id(cats)
                        provisional[source_key(arrived)] = (cat_end, next_id, arrived)
                        cat_end += 1
                        next_id += 1
                    cat_pos, cat_id, name = provisional[source_key(arrived)]
                position = item_end.get(cat_id, 0)
                item_end[cat_id] = position + 1
                spot = (cat_pos, cat_id, position, 0, name)
            spots[key] = spot
        keyed.append((spot[:4], index, row, spot))
    keyed.sort(key=lambda k: (k[0], k[1]))
    for _order, _index, row, spot in keyed:
        row.category_id = spot[1]
        row.category = spot[4]
    if unrecorded:
        answer_not_kept()
        log.warning('IPTV output: %s on the channel list had no place recorded, so a write path '
                    'missed recording it; listed at the end of its category until the next write: %s',
                    counted(len(unrecorded), 'row'), ', '.join(unrecorded))
    return [k[2] for k in keyed]
