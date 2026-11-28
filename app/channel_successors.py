"""Replacing a channel the playlist stopped listing with the row that replaced it.

When a playlist changes source or a provider renumbers, every channel comes back as a new row
and the old rows keep the group memberships, the guide row and the history. This module finds
the same stream on its new row, proposes it, and on the user's yes puts the new channel in the
old one's place and carries what the old one had (dev/docs/DESIGN-channel-successors.md,
dev/changelog/1794).

Three rules hold everything here:

  * **A proposal the user accepts, never something a sync does.** Unrelated backends share
    stream ids by chance (DESIGN-account-providers.md section 2), so nothing moves until a
    person says the two rows are the same channel.
  * **One writer per piece.** Every piece of carried state goes through the function that
    already owns it - set_participation(), set_guide_listings_member(), set_on_output(),
    set_manual_adjustment(), set_hidden_override(), set_channel_key(), set_source_override()
    - and a value the new row already has is never overwritten.
  * **The link is the record.** `channel_replacements` (written here only) says which row
    replaced which, and the new channel's health score reads the old one's observations
    through it (health_recompute.observation_ledger) rather than copying a number that the
    first reset would lose. A delete of the old row moves that history first
    (before_channel_delete), because channel ids are re-issued.
"""
import json
import logging
from datetime import datetime

from . import db
from .accounts import (channel_unlisted, last_sync_bounds, missing_channels_query,
                       normalize_url, unlisted_channel_expr)
from .channel_groups import (PARTICIPATION_FIELDS, set_guide_listings_member,
                             set_participation, touch_group)
from .database import (
    Account, Channel, ChannelEvent, ChannelGroup, ChannelGroupEvent, ChannelGroupMember,
    ChannelHealthExclusion, ChannelReplacement, ChannelTest, Recording, RuleMatch,
    add_recording_event,
    CHANNEL_ADDED_TO_GUIDE, CHANNEL_GROUPED, CHANNEL_HEALTH_HISTORY_CARRIED,
    CHANNEL_NEEDING_STATUSES, CHANNEL_REMOVED_FROM_GUIDE, CHANNEL_REPLACED, CHANNEL_UNGROUPED,
    EPG_KEY_ACCEPTED, EPG_KEY_REJECTED, GROUP_MEMBER_REPLACED, RECORDING_REPOINTED,
    REC_STATUS_SCHEDULED, REPLACE_MATCH_PICKED, REPLACE_MATCH_STREAM_ID, REPLACE_MATCH_URL,
)
from .db_utils import chunked, retry_on_locked
from .url_utils import mask_creds

log = logging.getLogger(__name__)

# ── What a replacement can carry (section 8.2) ──────────────────────────────────────────────
#
# Group membership and a guide-listings pin are not here: they are the replacement itself
# and always move (section 8.1).
CARRY_HEALTH = 'health'
CARRY_GUIDE = 'guide'
CARRY_NOTES = 'notes'
CARRY_ADJUSTMENT = 'adjustment'
CARRY_PROFILE = 'profile'
CARRY_PACE = 'pace'
CARRY_TEST_SWITCH = 'test_switch'
CARRY_HIDE = 'hide'
CARRY_EPG = 'epg'
CARRY_PIP = 'pip'
CARRY_SCHEDULED = 'scheduled'

CARRY_PIECES = (CARRY_HEALTH, CARRY_GUIDE, CARRY_NOTES, CARRY_ADJUSTMENT, CARRY_PROFILE,
                CARRY_PACE, CARRY_TEST_SWITCH, CARRY_HIDE, CARRY_EPG, CARRY_PIP,
                CARRY_SCHEDULED)

CARRY_LABELS = {
    CARRY_HEALTH: 'health history',
    CARRY_GUIDE: 'own guide row',
    CARRY_NOTES: 'notes',
    CARRY_ADJUSTMENT: 'manual health adjustment',
    CARRY_PROFILE: 'default recording profile',
    CARRY_PACE: 'pace',
    CARRY_TEST_SWITCH: 'health check switch',
    CARRY_HIDE: 'hide setting',
    CARRY_EPG: 'EPG choices',
    CARRY_PIP: 'picture-in-picture channels',
    CARRY_SCHEDULED: 'scheduled recordings',
}

#: The three Remove Duplicate Channels paths carry what they always carried, through the same
#: writers (section 10): no link and no health history, since the two rows are one stream at
#: the same time rather than an old row and its successor.
DEDUP_PIECES = frozenset({CARRY_GUIDE, CARRY_TEST_SWITCH, CARRY_SCHEDULED})

# What happened to one piece, as `ChannelReplacement.carried` and the events record it. A
# piece the old channel had nothing for is left out of the record altogether.
CARRIED = 'carried'
KEPT_OWN = 'kept_own'       # the new channel has its own value, which is never overwritten
NOT_ASKED = 'not_asked'     # the user left the piece unchecked
WAITING = 'waiting'         # could not be done now (an EPG source mid-refresh); says why

#: `ChannelHealthExclusion.action` for a recording's post-process check that landed after the
#: accept, when the recording moves onto the new channel at a delete (_move_history).
EXCLUDED_PAST_CUTOFF = 'past_cutoff'

#: Where the accept came from, for the events.
SURFACE_REVIEW = 'review'
SURFACE_CHANNEL = 'channel_page'
SURFACE_REPOINT = 'repoint'
SURFACE_DEDUP = 'dedup'


class ReplaceRefused(ValueError):
    """An accept the server will not do, with the sentence to show the user."""


# ── Detection and matching (sections 3 and 4) ───────────────────────────────────────────────

def replaced_old_ids(channel_ids=None) -> set:
    """Ids of channels that are the old side of a live replacement."""
    q = db.session.query(ChannelReplacement.old_channel_id).filter(
        ChannelReplacement.old_channel_id.isnot(None))
    if channel_ids is not None:
        ids = list(channel_ids)
        if not ids:
            return set()
        q = q.filter(ChannelReplacement.old_channel_id.in_(ids))
    return {cid for (cid,) in q.all()}


def stranded_channels(bounds=None, *, group_id=None):
    """Channels that are unlisted (accounts.channel_unlisted) and still hold something a
    replacement would move - a group membership or a guide row - and have not been replaced.
    One query, whatever the size: a page reads it once (section 4, Cost)."""
    if bounds is None:
        bounds = last_sync_bounds()
    if not bounds:
        return []
    members = db.session.query(ChannelGroupMember.channel_id)
    if group_id is not None:
        members = members.filter(ChannelGroupMember.group_id == group_id)
    holds = Channel.id.in_(members) if group_id is not None else db.or_(
        Channel.id.in_(members),
        Channel.in_guide.is_(True))  # guide-flag-read-ok: a guide row of its own is one of the two things a replacement moves
    replaced = db.session.query(ChannelReplacement.old_channel_id).filter(
        ChannelReplacement.old_channel_id.isnot(None))
    return (Channel.query.filter(unlisted_channel_expr(bounds), holds,
                                 Channel.id.notin_(replaced))
            .order_by(Channel.name, Channel.id).all())


def _provider_of(account_ids) -> dict:
    ids = list(account_ids)
    if not ids:
        return {}
    return dict(db.session.query(Account.id, Account.provider_id).filter(Account.id.in_(ids)).all())


def match_kind(old, cand, providers) -> str | None:
    """Why `cand` is the same stream as `old`, or None (section 4). The provider key needs both
    ids and either one account or two accounts on one provider; the URL key is today's Re-point
    match and works on any account. `Channel.stream_id` is never a key."""
    if (old.provider_stream_id and cand.provider_stream_id == old.provider_stream_id
            and (cand.account_id == old.account_id
                 or (providers.get(old.account_id) is not None
                     and providers.get(old.account_id) == providers.get(cand.account_id)))):
        return REPLACE_MATCH_STREAM_ID
    if cand.stream_url == old.stream_url:
        return REPLACE_MATCH_URL
    return None


def proposals(olds, bounds=None) -> dict:
    """{old channel id: [(candidate Channel, match kind), ...]} for every channel in `olds`.

    A candidate is listed, is not the old channel, is not itself the old side of a
    replacement, and is the same stream by match_kind(). Two queries for the candidates
    however many old channels there are (provider stream ids, then URLs), chunked, never one
    per channel. Sorted by account then name, so a page and the server see one order."""
    olds = list(olds)
    if not olds:
        return {}
    if bounds is None:
        bounds = last_sync_bounds()
    psids = sorted({c.provider_stream_id for c in olds if c.provider_stream_id})
    urls = sorted({c.stream_url for c in olds if c.stream_url})
    # NOT of an unknown is unknown in SQL, so a NULL last_seen_at is named as listed here, the
    # way channel_unlisted() reads it.
    listed = (db.or_(Channel.last_seen_at.is_(None), db.not_(unlisted_channel_expr(bounds)))
              if bounds else db.true())
    found = {}
    for column, values in ((Channel.provider_stream_id, psids), (Channel.stream_url, urls)):
        for chunk in chunked(values, 400):
            for ch in Channel.query.filter(column.in_(chunk), listed).all():
                found[ch.id] = ch
    taken = replaced_old_ids(found.keys())
    providers = _provider_of({c.account_id for c in olds} | {c.account_id for c in found.values()})
    out = {}
    for old in olds:
        rows = []
        for cand in found.values():
            if cand.id == old.id or cand.id in taken:
                continue
            kind = match_kind(old, cand, providers)
            if kind is not None:
                rows.append((cand, kind))
        rows.sort(key=lambda r: (r[0].account_id != old.account_id, r[0].account_id,
                                 (r[0].name or '').lower(), r[0].id))
        out[old.id] = rows
    return out


def for_target(rows, old, target):
    """The proposals the review's "Replace with channels from" choice leaves (section 4):
    None is every one, 'own' the old channel's own account, an int that account."""
    if target is None:
        return list(rows)
    want = old.account_id if target == 'own' else int(target)
    return [r for r in rows if r[0].account_id == want]


def single_match_pairs(props, olds, target):
    """[(old id, new id)] for every old channel with exactly one proposal under `target` -
    what Accept all accepts. A channel with several is left for the user to choose, and one
    with none for the user to pick (section 4): neither is ever chosen for them."""
    pairs = []
    for old in olds:
        rows = for_target(props.get(old.id, []), old, target)
        if len(rows) == 1:
            pairs.append((old.id, rows[0][0].id))
    return pairs


# ── The replacement itself (section 8) ──────────────────────────────────────────────────────

def _account_name(channel):
    return channel.account.name if channel.account is not None else 'a deleted account'


def _who(channel):
    return f'"{channel.name}" ({_account_name(channel)})'


def _recording_enabled_counts(group_ids):
    if not group_ids:
        return {}
    rows = (db.session.query(ChannelGroupMember.group_id, db.func.count(ChannelGroupMember.id))
            .filter(ChannelGroupMember.group_id.in_(list(group_ids)),
                    ChannelGroupMember.recording_enabled.is_(True))
            .group_by(ChannelGroupMember.group_id).all())
    return {gid: n for gid, n in rows}


def _move_memberships(old, new, result):
    """Section 8.1: every group the old channel is in. A swap changes the member row's channel
    in place, so its position and both switches are the member's and are not written; a merge
    (the new channel is already in the group) removes the old membership and carries each
    switch that was on for it and off for the new one through set_participation(). Nothing is
    turned off either way. A merge of two recording-enabled members leaves one where there were
    two, but it never leaves a group that had one with none, which is the guide invariant -
    asserted, not assumed."""

    memberships = list(old.group_memberships)
    group_ids = {m.group_id for m in memberships}
    before = _recording_enabled_counts(group_ids)
    existing = {m.group_id: m for m in ChannelGroupMember.query.filter(
        ChannelGroupMember.channel_id == new.id,
        ChannelGroupMember.group_id.in_(list(group_ids))).all()} if group_ids else {}
    for m in memberships:
        group = m.group
        touch_group(group)
        kept = existing.get(m.group_id)
        if kept is None:
            m.channel = new
            merged = False
        else:
            for field in PARTICIPATION_FIELDS:
                if getattr(m, field) and not getattr(kept, field):
                    set_participation(kept, field, True, surface='replace')
            db.session.delete(m)  # hidden-recompute-ok: replace() recomputes both channels after every move
            merged = True
        detail = f'{_who(old)} replaced by {_who(new)}'
        if merged:
            detail += (f'. {new.name} was already in the group, so the two were merged and '
                       'any switch that was on for the old one is on for it')
        db.session.add(ChannelGroupEvent(
            group_id=group.id, channel_id=new.id, event_type=GROUP_MEMBER_REPLACED,
            detail=detail, extra_data=json.dumps({
                'old_channel_id': old.id, 'new_channel_id': new.id, 'merged': merged,
                'surface': result['surface']})))
        db.session.add(ChannelEvent(
            channel_id=old.id, event_type=CHANNEL_UNGROUPED,
            detail=f'Removed from channel group "{group.name}" - replaced by {_who(new)}'))
        if not merged:
            db.session.add(ChannelEvent(
                channel_id=new.id, event_type=CHANNEL_GROUPED,
                detail=f'Added to channel group "{group.name}" in place of {_who(old)}'))
        result['groups'].append({'id': group.id, 'name': group.name, 'merged': merged})
    db.session.flush()
    # The member rows moved under both channels' collections; read them again from the table.
    db.session.expire(old, ['group_memberships'])
    db.session.expire(new, ['group_memberships'])
    for gid in group_ids:
        group = db.session.get(ChannelGroup, gid)
        db.session.expire(group, ['memberships'])
        if group.guide_listings_channel_id == old.id:
            set_guide_listings_member(group, new, reason='replaced')
    after = _recording_enabled_counts(group_ids)
    emptied = [gid for gid in group_ids if before.get(gid, 0) and not after.get(gid, 0)]
    if emptied:
        raise RuntimeError(f'A replacement left groups {emptied} with nothing switched on for '
                           'recording; it only ever keeps or turns on switches')


def _carry_guide(old, new, result):
    """The old channel's own guide row (section 8.2): the new one takes it, in the same sort
    slot, and the old one gives it up either way, since a row on a channel the playlist no
    longer lists records nothing. The IPTV output switch goes with the row (section 8.3)."""
    if not old.in_guide:
        return None
    from .output_arrangement import carry_item
    from .output_feed import KIND_CHANNEL, feed_id_for_channel, set_on_output
    # Its place on the IPTV output too, so the player's number and folder hold; a new channel
    # that already had a place keeps its own (DESIGN-output-feed.md 16.1, dev/changelog/1797).
    carry_item(feed_id_for_channel(old.id), feed_id_for_channel(new.id))
    state = KEPT_OWN
    if not new.in_guide:
        new.in_guide = True  # hidden-recompute-ok: replace() recomputes both channels after every move
        new.guide_sort_order = old.guide_sort_order
        db.session.add(ChannelEvent(
            channel_id=new.id, event_type=CHANNEL_ADDED_TO_GUIDE,
            detail=f'Added to guide at position {new.guide_sort_order}, in place of {_who(old)}'))
        if bool(old.on_output) != bool(new.on_output):
            set_on_output(KIND_CHANNEL, new.id, bool(old.on_output),
                          why=f'carried over from {_who(old)}, which it replaced')
        result['on_output'] = bool(old.on_output)
        state = CARRIED
    old.in_guide = False  # hidden-recompute-ok: replace() recomputes both channels after every move
    db.session.add(ChannelEvent(
        channel_id=old.id, event_type=CHANNEL_REMOVED_FROM_GUIDE,
        detail=f'Removed from guide - {_who(new)} took its place'))
    result['old_feed_ids'].append(old.id)
    return state


def _carry_epg(old, new, cfg, result):
    """EPG keys and the source override (section 8.2), each only into a source the new
    channel's account reads and where the new channel has no answer of its own. An ignored key
    names the old row's own provider id and means nothing on another row, so it stays."""
    from .database import EpgChannelKey, EpgSource
    from .epg_sources import (case_sensitive_matching, refreshing_source_names,
                              reject_name_match, set_channel_key, set_source_override,
                              subscriptions_for)
    keys = EpgChannelKey.query.filter(
        EpgChannelKey.channel_id == old.id,
        EpgChannelKey.status.in_([EPG_KEY_ACCEPTED, EPG_KEY_REJECTED])).all()
    override_id = old.epg_source_override_id
    if not keys and override_id is None:
        return None
    reads = set(subscriptions_for([new.account_id]).get(new.account_id, []))
    case_sensitive = case_sensitive_matching(cfg)
    have = {k.source_id for k in EpgChannelKey.query.filter_by(channel_id=new.id).all()}
    states = []
    for key in keys:
        source = db.session.get(EpgSource, key.source_id)
        if source is None or source.id not in reads:
            continue
        if source.id in have:
            states.append(KEPT_OWN)
            continue
        if refreshing_source_names([new.account_id], [source.id]):
            states.append(WAITING)
            result['waiting'].append(f'EPG source "{source.name}" is refreshing')
            continue
        if key.status == EPG_KEY_ACCEPTED:
            set_channel_key(new, source, key.key, case_sensitive=case_sensitive,
                            origin=key.origin, matched_on=key.matched_on)
        else:
            reject_name_match(new.id, source.id, key.matched_on)
        result['epg_touched'] = True
        states.append(CARRIED)
    if override_id is not None and override_id in reads:
        if new.epg_source_override_id is not None:
            states.append(KEPT_OWN)
        elif refreshing_source_names([new.account_id], [override_id]):
            states.append(WAITING)
            result['waiting'].append('the chosen EPG source is refreshing')
        else:
            set_source_override(new, db.session.get(EpgSource, override_id),
                                case_sensitive=case_sensitive)
            result['epg_touched'] = True
            states.append(CARRIED)
    if not states:
        return None
    for state in (CARRIED, WAITING, KEPT_OWN):
        if state in states:
            return state
    return None


def _move_scheduled(old, new, cfg, *, plain, moved_groups, result):
    """Scheduled recordings that name the old channel (section 8.2). `plain` moves the ones on
    the channel itself (the scheduled piece); a group recording stamped to the old member moves
    whenever its group moved, so the old row does not hold up a later delete. A recording that
    is running or over is never touched. The rule ledger's picks move with their recording."""
    rows = Recording.query.filter(Recording.channel_id == old.id,
                                  Recording.status == REC_STATUS_SCHEDULED).all()
    fresh_url = normalize_url(new.stream_url, new.account, cfg)
    moved, plain_moved = [], []
    for rec in rows:
        if rec.group_id is None and not plain:
            continue
        if rec.group_id is not None and rec.group_id not in moved_groups:
            continue
        old_url = rec.url
        rec.channel_id = new.id
        rec.url = fresh_url
        add_recording_event(
            rec.id, RECORDING_REPOINTED,
            detail=(f'Channel {_who(old)} replaced by {_who(new)}; the recording moved with it '
                    f'({mask_creds(old_url)} -> {mask_creds(fresh_url)})'),
            extra={'old_channel_id': old.id, 'new_channel_id': new.id,
                   'old_url': mask_creds(old_url), 'new_url': mask_creds(fresh_url)})
        moved.append(rec.id)
        if rec.group_id is None:
            plain_moved.append(rec.id)
    # The listing a scheduled recording was made from names the channel too; a moved group
    # recording's listing comes from the same channel.
    Recording.query.filter(Recording.source_channel_id == old.id,
                           Recording.status == REC_STATUS_SCHEDULED,
                           db.or_(Recording.id.in_(moved) if moved else db.false(),
                                  Recording.group_id.in_(list(moved_groups))
                                  if moved_groups else db.false())
                           ).update({'source_channel_id': new.id}, synchronize_session=False)
    if moved:
        RuleMatch.query.filter(RuleMatch.channel_id == old.id,
                               RuleMatch.recording_id.in_(moved)).update(
            {'channel_id': new.id}, synchronize_session=False)
    result['moved_recordings'] = moved
    return plain_moved


def replace(old, new, options, surface, *, cfg, link=None, health=None):
    """Put `new` in `old`'s place and carry what `options` asks for. Mutates and adds events;
    does NOT commit (the caller's retry_on_locked unit owns that, with no non-idempotent side
    effect inside it). `options` is the set of CARRY_* pieces left checked. Returns a dict the
    caller hands to after_commit() and summary(): what moved, what each piece did, and what
    must happen once the commit has landed.

    Not here, because they are the accept's: the link row, the health recompute and the
    validation. The Remove Duplicate Channels paths call this with DEDUP_PIECES and no link.
    `health` is the accept's answer for the health piece, recorded with the rest so the events
    name it."""
    options = set(options or ())
    result = {
        'surface': surface, 'old_id': old.id, 'new_id': new.id, 'groups': [],
        'carried': {}, 'waiting': [], 'pip_rows': set(), 'old_feed_ids': [],
        'on_output': False, 'moved_recordings': [], 'epg_touched': False,
    }
    carried = result['carried']
    if health is not None:
        carried[CARRY_HEALTH] = health

    def record(piece, state):
        if state is not None:
            carried[piece] = state if piece in options else NOT_ASKED

    _move_memberships(old, new, result)
    moved_groups = {g['id'] for g in result['groups']}

    if CARRY_GUIDE in options:
        record(CARRY_GUIDE, _carry_guide(old, new, result))
    elif old.in_guide:
        record(CARRY_GUIDE, NOT_ASKED)

    if old.notes:
        if CARRY_NOTES in options and not new.notes:
            new.notes = old.notes
        record(CARRY_NOTES, KEPT_OWN if new.notes and new.notes != old.notes else CARRIED)

    if old.manual_health_adjustment or old.manual_health_note:
        state = KEPT_OWN if (new.manual_health_adjustment or new.manual_health_note) else CARRIED
        if CARRY_ADJUSTMENT in options and state == CARRIED:
            from .health_score import set_manual_adjustment
            set_manual_adjustment(new, old.manual_health_adjustment or 0, old.manual_health_note,
                                  why=f'carried over from {_who(old)}, which it replaced',
                                  extra={'carried_from': old.id})
        record(CARRY_ADJUSTMENT, state)

    if old.default_profile_id is not None:
        state = KEPT_OWN if new.default_profile_id is not None else CARRIED
        if CARRY_PROFILE in options and state == CARRIED:
            new.default_profile_id = old.default_profile_id
        record(CARRY_PROFILE, state)

    if old.pace_realtime is not None:
        state = KEPT_OWN if new.pace_realtime is not None else CARRIED
        if CARRY_PACE in options and state == CARRIED:
            new.pace_realtime = old.pace_realtime
        record(CARRY_PACE, state)

    if old.test_enabled and not new.test_enabled:
        if CARRY_TEST_SWITCH in options:
            # participation-write-ok: Channel.test_enabled, the channel-wide switch, not the
            # group-scoped ChannelGroupMember column; only ever turned on.
            new.test_enabled = True
        record(CARRY_TEST_SWITCH, CARRIED)

    if old.hidden_override is not None:
        state = KEPT_OWN if new.hidden_override is not None else CARRIED
        if CARRY_HIDE in options and state == CARRIED:
            from .channel_hiding import set_hidden_override
            set_hidden_override(new, bool(old.hidden_override), surface='replace')
        record(CARRY_HIDE, state)

    if CARRY_EPG in options:
        record(CARRY_EPG, _carry_epg(old, new, cfg, result))
    elif old.epg_source_override_id is not None:
        record(CARRY_EPG, NOT_ASKED)

    if CARRY_PIP in options:
        from .pip_channels import repoint_refs
        touched = repoint_refs(old.id, new.id)
        result['pip_rows'] = touched
        if touched:
            record(CARRY_PIP, CARRIED)

    plain_moved = _move_scheduled(old, new, cfg, plain=CARRY_SCHEDULED in options,
                                  moved_groups=moved_groups, result=result)
    if plain_moved:
        record(CARRY_SCHEDULED, CARRIED)
    elif CARRY_SCHEDULED not in options and Recording.query.filter(
            Recording.channel_id == old.id, Recording.status == REC_STATUS_SCHEDULED,
            Recording.group_id.is_(None)).count():
        record(CARRY_SCHEDULED, NOT_ASKED)

    # Losing the last group or guide row can let a deferred hide take effect on the old
    # channel, and gaining one protects the new; the recompute also re-ranks both duplicate
    # clusters, which the after-flush listener does not see for a reassigned membership.
    from . import channel_hiding
    channel_hiding.recompute([old.id, new.id])

    if link is not None:
        result['link_id'] = link.id
        words = summary(result)
        for side, channel, other in (('old', old, new), ('new', new, old)):
            detail = (f'Replaced by {_who(new)}' if side == 'old'
                      else f'Took the place of {_who(old)}') + f' - {words}'
            db.session.add(ChannelEvent(
                channel_id=channel.id, event_type=CHANNEL_REPLACED, detail=detail,
                extra_data=json.dumps({'role': side, 'other_channel_id': other.id,
                                       'replacement_id': link.id, 'surface': surface,
                                       'carried': carried, 'groups': result['groups']})))
    return result


def summary(result) -> str:
    """One sentence of what a replacement did, for a toast, an event and the Re-point reply."""
    parts = []
    groups = result['groups']
    if groups:
        names = ', '.join(f'"{g["name"]}"' for g in groups)
        parts.append(f'took its place in {names}')
    carried = [CARRY_LABELS[p] for p in CARRY_PIECES if result['carried'].get(p) == CARRIED]
    if carried:
        parts.append('brought over ' + ', '.join(carried))
    kept = [CARRY_LABELS[p] for p in CARRY_PIECES if result['carried'].get(p) == KEPT_OWN]
    if kept:
        parts.append('kept its own ' + ', '.join(kept))
    if result['waiting']:
        parts.append('not yet: ' + '; '.join(result['waiting']))
    return '; '.join(parts) if parts else 'nothing to move'


def after_commit(results) -> None:
    """The side effects of a replacement, run once its commit has landed and never inside the
    retried unit (section 10): the format re-evaluation of each group a member moved in, the
    picture-in-picture refresh, ending output plays on the old channel's feed id, and the EPG
    findings recheck. Each is logged and never undoes the replacement."""
    if isinstance(results, dict):
        results = [results]
    groups, pip_rows, feed_ids, epg = set(), set(), set(), False
    for r in results:
        groups.update(g['id'] for g in r['groups'])
        pip_rows.update(r['pip_rows'])
        feed_ids.update(r['old_feed_ids'])
        epg = epg or r['epg_touched']
    from .channel_groups import DEFAULT_FAILING_STREAK_THRESHOLD, evaluate_and_reconcile_group
    if groups:
        from .config import load_config
        streak = load_config().get('channel_testing', {}).get(
            'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
    for gid in sorted(groups):
        group = db.session.get(ChannelGroup, gid)
        if group is None:
            continue
        try:
            evaluate_and_reconcile_group(group, streak_threshold=streak)
        except Exception:                               # noqa: BLE001 - never undoes the replacement
            log.exception('Group %d: format re-evaluation after a replacement failed', gid)
    from . import output_feed
    for row_id in sorted(pip_rows):
        try:
            output_feed.refresh_pip(row_id)
        except Exception:                               # noqa: BLE001 - one row's plays, never the replacement
            log.exception('Picture-in-picture channel %d: plays not refreshed after a replacement',
                          row_id)
    try:
        for channel_id in sorted(feed_ids):
            output_feed.stop_feed_id(output_feed.feed_id_for_channel(channel_id),
                                    output_feed.REASON_TAKEN_OFF)
        if feed_ids:
            output_feed.forget_answers()
    except Exception:                                   # noqa: BLE001 - never undoes the replacement
        log.exception('Output plays of replaced channels %s not ended after a replacement',
                      sorted(feed_ids))
    if epg:
        from .epg_scorecard import recheck_soon
        try:
            recheck_soon('a replacement carried EPG choices to another channel')
        except Exception:                               # noqa: BLE001 - never undoes the replacement
            log.exception('EPG findings recheck not queued after a replacement')


# ── The accept (section 10) ─────────────────────────────────────────────────────────────────

def _undo_link(old, new):
    """The live link that says `new` was replaced by `old` - replacing back is the undo."""
    return ChannelReplacement.query.filter_by(old_channel_id=new.id, new_channel_id=old.id).first()


def pairing_problem(old, new, *, picked, cfg, bounds=None):
    """Why `old` may not be replaced by `new` right now, or None. Re-derived from database ids
    on every accept: the server never trusts the client's claim that a pairing is a proposal.
    Returns (problem, match_kind)."""
    if old is None or new is None:
        return 'That channel no longer exists.', None
    if old.id == new.id:
        return 'A channel cannot replace itself.', None
    # Before the undo: B replaced by C cannot be undone back onto A (A by B), or C would stop
    # counting A's history with nothing said.
    first = ChannelReplacement.query.filter_by(old_channel_id=old.id).first()
    if first is not None:
        other = db.session.get(Channel, first.new_channel_id)
        return (f'"{old.name}" was already replaced by '
                f'"{other.name if other else "a deleted channel"}".'), None
    if _undo_link(old, new) is not None:
        return None, None
    if ChannelReplacement.query.filter_by(old_channel_id=new.id).first() is not None:
        return f'"{new.name}" has itself been replaced, so it cannot take another\'s place.', None
    if bounds is None:
        bounds = last_sync_bounds([old.account_id, new.account_id])
    missing = missing_channels_query(None, cfg, channel_ids=[old.id, new.id]).all()
    missing_ids = {c.id for c in missing}
    if not (channel_unlisted(old, bounds) or old.id in missing_ids):
        return f'"{old.name}" is still listed by its account\'s last sync.', None
    if channel_unlisted(new, bounds) or new.id in missing_ids:
        return f'"{new.name}" was not listed by its account\'s last sync either.', None
    kind = match_kind(old, new, _provider_of({old.account_id, new.account_id}))
    if kind is None:
        if not picked:
            return (f'"{new.name}" is not the same stream as "{old.name}"; pick it by hand '
                    'to replace it anyway.'), None
        kind = REPLACE_MATCH_PICKED
    return None, kind


def _write_link(old, new, kind, health, now):
    link = ChannelReplacement(
        old_channel_id=old.id, new_channel_id=new.id, accepted_at=now, match_kind=kind,
        health_carried=bool(health), old_name=old.name, old_account_name=_account_name(old))
    db.session.add(link)
    db.session.flush()
    return link


def _health_event(channel, before, after, on, link_id, old_name, why):
    from .health_recompute import _score_words
    detail = (f'Health score {"now counts" if on else "no longer counts"} the history of '
              f'"{old_name}" ({why}) - {_score_words(before, after)}')
    db.session.add(ChannelEvent(
        channel_id=channel.id, event_type=CHANNEL_HEALTH_HISTORY_CARRIED, detail=detail,
        extra_data=json.dumps({'carried': on, 'replacement_id': link_id,
                               'score_before': before, 'score_after': after})))


def _rescore(channel, cfg):
    """Replay `channel`'s score from its ledger as the link now stands. Returns (before,
    after). A channel with nothing to replay keeps no score, which is what a ledger with no
    observations means."""
    from .health_recompute import _rewrite_from_ledger, observation_ledger
    before = channel.health_score
    _rewrite_from_ledger(channel, observation_ledger(channel.id, cfg), cfg)
    return before, channel.health_score


def accept(old_id, new_id, options, *, cfg, surface=SURFACE_REVIEW, picked=False):
    """Replace one channel: one retry_on_locked unit with one commit - re-fetch both rows,
    check the pairing, replace(), the link row, the recompute - then the side effects after
    it. Raises ReplaceRefused with the sentence to show. Returns replace()'s result plus the
    link id and the new channel's score before and after.

    Replacing back (B by A after A by B) is the undo: the A-to-B link goes, B's score is
    replayed without A's history, and the swap and carry run the other way under the same
    rules, with no new link (section 5)."""
    options = set(options or ())

    @retry_on_locked()
    def _accept_and_commit():
        old = db.session.get(Channel, old_id)
        new = db.session.get(Channel, new_id)
        problem, kind = pairing_problem(old, new, picked=picked, cfg=cfg)
        if problem:
            raise ReplaceRefused(problem)
        undo = _undo_link(old, new)
        now = datetime.utcnow()
        if undo is not None:
            was_carried = undo.health_carried
            db.session.delete(undo)
            db.session.flush()
            result = replace(old, new, options - {CARRY_HEALTH}, surface, cfg=cfg)
            if was_carried:
                before, after = _rescore(old, cfg)
                _health_event(old, before, after, False, None, new.name,
                              'the replacement was undone')
            words = summary(result)
            for channel, other, role in ((old, new, 'old'), (new, old, 'new')):
                db.session.add(ChannelEvent(
                    channel_id=channel.id, event_type=CHANNEL_REPLACED,
                    detail=(f'Replacement undone: {_who(new)} is back in place of {_who(old)} '
                            f'- {words}'),
                    extra_data=json.dumps({'role': role, 'other_channel_id': other.id,
                                           'undo': True, 'surface': surface,
                                           'carried': result['carried']})))
            result.update(link_id=None, undo=True, score_before=new.health_score,
                          score_after=new.health_score)
            db.session.commit()
            return result
        health = CARRY_HEALTH in options
        link = _write_link(old, new, kind, health, now)
        # The score first: it reads only the link, and the events replace() writes then name
        # the health piece with the rest.
        before = after = new.health_score
        if health:
            before, after = _rescore(new, cfg)
            _health_event(new, before, after, True, link.id, old.name, 'it replaced that channel')
        from .health_recompute import observation_ledger
        has_history = bool(old.health_score_sample_count
                           or observation_ledger(old.id, cfg, through_links=False))
        state = (CARRIED if health else NOT_ASKED) if has_history else None
        result = replace(old, new, options, surface, cfg=cfg, link=link, health=state)
        link.carried = json.dumps(result['carried'])
        result.update(link_id=link.id, undo=False, match_kind=kind,
                      score_before=before, score_after=after)
        db.session.commit()
        return result

    result = _accept_and_commit()
    after_commit(result)
    log.info('Channel %d replaced by channel %d (%s): %s', old_id, new_id, surface,
             summary(result))
    return result


def accept_many(pairs, options, *, cfg, surface=SURFACE_REVIEW):
    """A bulk accept: a loop of accept() units, never one commit over many. `pairs` is
    [(old_id, new_id)]. Cut short, it is simply run again - an accepted channel is no longer
    stranded and is refused a second time. Returns (replaced results, [(old_id, reason)])."""
    done, skipped = [], []
    for old_id, new_id in pairs:
        try:
            done.append(accept(old_id, new_id, options, cfg=cfg, surface=surface))
        except ReplaceRefused as err:
            skipped.append((old_id, str(err)))
    return done, skipped


def set_health_carried(link_id, on, *, cfg):
    """Turn the link's health switch on or off from the new channel's page (section 6): the
    score is replayed with or without the old channel's history, and the move says so. Returns
    (before, after), or None when the switch did not move or the link is gone."""
    @retry_on_locked()
    def _switch_and_commit():
        link = db.session.get(ChannelReplacement, link_id)
        if link is None or bool(link.health_carried) == bool(on):
            return None
        link.health_carried = bool(on)
        db.session.flush()
        new = db.session.get(Channel, link.new_channel_id)
        before, after = _rescore(new, cfg)
        _health_event(new, before, after, bool(on), link.id, link.old_name, 'switched by hand')
        db.session.commit()
        return before, after

    return _switch_and_commit()


# ── The health chain (section 6) ────────────────────────────────────────────────────────────

def carried_chain(channel_id):
    """[(old channel id, cutoff, old name)] whose observations `channel_id`'s ledger counts:
    each live link into it with health_carried on, and theirs, down the chain, each cutoff the
    earliest accept on the way down. One query per hop; a cycle cannot form (replacing back
    removes the link) but is guarded anyway."""
    chain = []
    seen = {channel_id}
    frontier = [(channel_id, None)]
    while frontier:
        ids = [cid for cid, _ in frontier]
        cutoff_of = dict(frontier)
        links = ChannelReplacement.query.filter(
            ChannelReplacement.new_channel_id.in_(ids),
            ChannelReplacement.old_channel_id.isnot(None),
            ChannelReplacement.health_carried.is_(True)).all()
        frontier = []
        for link in links:
            if link.old_channel_id in seen:
                continue
            seen.add(link.old_channel_id)
            parent = cutoff_of[link.new_channel_id]
            cutoff = link.accepted_at if parent is None else min(parent, link.accepted_at)
            chain.append((link.old_channel_id, cutoff, link.old_name))
            frontier.append((link.old_channel_id, cutoff))
    return chain


# ── Deleting either row later (section 7) ───────────────────────────────────────────────────

def _move_history(old_id, new_id, cutoff, cfg):
    """Re-home the rows old_id's link was counting onto new_id: the observations at or before
    the cutoff (tests, the four health-observation events, finished recordings) and every
    exclusion. Read from old_id's own ledger, so exactly what the link counted moves."""
    from .database import ChannelEvent as CE
    from .health_recompute import (RECORDING_SOURCE_KINDS, SOURCE_TEST, observation_ledger)
    ledger = observation_ledger(old_id, cfg, through_links=False)
    tests, events, recordings, late = set(), set(), set(), []
    for o in ledger:
        if o.observed_at > cutoff:
            if o.kind in RECORDING_SOURCE_KINDS:
                late.append(o)
            continue
        if o.kind == SOURCE_TEST:
            tests.add(o.source_id)
        elif o.kind in RECORDING_SOURCE_KINDS:
            recordings.add(o.source_id)
        else:
            events.add(o.source_id)
    for chunk in chunked(sorted(tests), 500):
        ChannelTest.query.filter(ChannelTest.id.in_(chunk), ChannelTest.channel_id == old_id).update(
            {'channel_id': new_id}, synchronize_session=False)
    for chunk in chunked(sorted(events), 500):
        CE.query.filter(CE.id.in_(chunk), CE.channel_id == old_id).update(
            {'channel_id': new_id}, synchronize_session=False)
    for chunk in chunked(sorted(recordings), 500):
        Recording.query.filter(Recording.id.in_(chunk), Recording.channel_id == old_id,
                               Recording.status.notin_(CHANNEL_NEEDING_STATUSES)).update(
            {'channel_id': new_id}, synchronize_session=False)
    held = {(e.source_kind, e.source_id) for e in
            ChannelHealthExclusion.query.filter_by(channel_id=new_id).all()}
    for e in ChannelHealthExclusion.query.filter_by(channel_id=old_id).all():
        if (e.source_kind, e.source_id) in held:
            db.session.delete(e)   # the new channel already holds the same one
        else:
            e.channel_id = new_id
            held.add((e.source_kind, e.source_id))
    # A recording moves whole, but its post-process check can have landed after the accept,
    # which the link never counted. It stays not counted on the new channel, marked as such,
    # so the score is the same at the next recompute.
    now = datetime.utcnow()
    for o in late:
        if o.source_id in recordings and o.key not in held:
            db.session.add(ChannelHealthExclusion(
                channel_id=new_id, source_kind=o.kind, source_id=o.source_id,
                excluded_at=now, action=EXCLUDED_PAST_CUTOFF))
            held.add(o.key)
    return len(tests) + len(events) + len(recordings)


def before_channel_delete(channel_ids, cfg) -> int:
    """Clear or re-home every link that names one of `channel_ids`, which are about to be
    deleted, inside the delete's own unit and before detach_channel_references() and the bulk
    deletes (section 7). Both delete paths call it: the missing-channel delete and an account
    delete. Mutates without committing. Returns how many links it touched.

      * An old channel with a live link whose health is carried: what the link was counting
        moves to the new channel, so its score is unchanged at the next recompute, and
        old_channel_id goes NULL - the link stays as the record.
      * The same with the health not carried: nothing moves; old_channel_id goes NULL.
      * A new channel: its link rows go, and the old channel reads as unlisted again - unless
        the deleted channel was itself replaced (A by B, B by C, B deleted), when A's link is
        re-pointed to C with its own cutoff, counting A only where both links counted. When A
        goes in the same delete, A's history then moves to C like any old channel's.
    """
    ids = set(channel_ids)
    if not ids:
        return 0
    db.session.flush()
    touched = 0
    as_old, as_new = [], []
    for chunk in chunked(sorted(ids), 500):
        as_old += ChannelReplacement.query.filter(ChannelReplacement.old_channel_id.in_(chunk)).all()
        as_new += ChannelReplacement.query.filter(ChannelReplacement.new_channel_id.in_(chunk)).all()
    successor = {link.old_channel_id: link for link in as_old}
    gone = set()
    for link in as_new:
        # A by B, B by C, B deleted (and A too, perhaps): A's link now ends at the first channel
        # down the chain that survives, counting A only where every link on the way counted,
        # with A's own cutoff (always the earliest accept).
        carried, end, seen = bool(link.health_carried), link.new_channel_id, set()
        while end is not None and end in ids:
            seen.add(end)
            onward = successor.get(end)
            if onward is None or onward.new_channel_id in seen:
                end = None
                break
            carried = carried and bool(onward.health_carried)
            end = onward.new_channel_id
        if end is not None:
            link.health_carried = carried
            link.new_channel_id = end
        else:
            db.session.delete(link)
            gone.add(link.id)
        touched += 1
    for link in as_old:
        if link.id in gone:
            continue
        if link.new_channel_id not in ids and link.health_carried:
            _move_history(link.old_channel_id, link.new_channel_id, link.accepted_at, cfg)
        link.old_channel_id = None
        touched += 1
    db.session.flush()
    # An account delete removes channels through the ORM cascade, which would act on any
    # tests, events or exclusions it had already loaded for them; those moved.
    for obj in list(db.session.identity_map.values()):
        if isinstance(obj, Channel) and obj.id in ids:
            db.session.expire(obj, ['tests', 'events', 'health_exclusions'])
    return touched
