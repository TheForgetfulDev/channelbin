"""A recording's own copy of how its program describes itself.

`Recording.metadata_description` / `_category` / `_rating` hold the program's synopsis,
genre and content rating. They are copied off the `epg_entries` row when the recording is
scheduled and refreshed here from whatever that listing says at air time, because a
recording set up three days out may be describing a program the provider has since
revised.

They cannot simply be read from `epg_entries` when something needs them. That table is not
a durable store by design: `epg.epg_keep_days` deletes a program's row within a day of it
airing, and a sync replaces rows wholesale so ids do not survive one. By the time a
finished recording is being described to anything outside this app, its listing is
routinely already gone - which is why capturing these is the irreversible half and
rendering them is not (dev/changelog/1055).

This module is the canonical home for both halves of that: finding a recording's listing
again, and applying the refresh.
"""
import logging

from . import db
from .database import (
    EPGEntry, Recording, add_recording_event,
    RECORDING_METADATA_EDITED, RECORDING_METADATA_REFRESHED,
    RECORDING_METADATA_REFRESH_SKIPPED,
)
from .db_utils import retry_on_locked

log = logging.getLogger(__name__)

#: The columns this module owns, paired with the `EPGEntry` attribute each one mirrors.
#: One list, so the lookup, the comparison and the event's prose can never drift apart.
_FIELDS = (
    ('metadata_description', 'description', 'description'),
    ('metadata_category', 'category', 'category'),
    ('metadata_rating', 'rating', 'rating'),
)


def _candidate_channel_ids(rec):
    """The channel ids whose listings could hold this recording's program.

    For a channel-backed recording that is just its channel. For a GROUP-backed one it is
    every member of the group, because `channel_id` is re-resolved at record start to
    whichever member is serving - so by the time the refresh runs, the channel carrying
    the listing we snapshotted from is frequently not the channel we are recording.

    Deliberately NOT `channel_groups.guide_scope_channel_ids()`, despite that being the
    canonical answer to a question one word away from this one. That subquery is every
    in-guide channel plus every in-guide group's members, globally - it answers "can this
    channel's listings reach the guide at all", and using it here would let a program on
    an unrelated channel match. The group's own membership is the narrower, correct set,
    and reading it is not re-deriving guide scope.
    """
    if rec.group_id is not None and rec.group is not None:
        ids = [m.channel_id for m in rec.group.memberships]
        # The serving channel first, then the one this was scheduled against, so a program
        # carried identically by several members resolves the same way on every run.
        head = [c for c in (rec.channel_id,) if c is not None]
        return head + [c for c in ids if c not in head]
    return [rec.channel_id] if rec.channel_id is not None else []


def find_program_entry(rec):
    """This recording's program listing as it stands now, or None.

    Keyed on the channel plus the IMMUTABLE `program_start_time` snapshot, which is why
    no `source_epg_id` column is needed: the id does not survive a sync, the air time
    does. An exact start-time match is deliberate. A provider that moved the program has
    published a different airing, and describing a recording with the synopsis of whatever
    else now starts near that minute would be worse than leaving the snapshot alone.
    """
    channel_ids = _candidate_channel_ids(rec)
    if not channel_ids or rec.program_start_time is None:
        return None
    rows = EPGEntry.query.filter(
        EPGEntry.channel_id.in_(channel_ids),
        EPGEntry.start_time == rec.program_start_time,
    ).all()
    if not rows:
        return None
    by_channel = {r.channel_id: r for r in rows}
    for cid in channel_ids:
        if cid in by_channel:
            return by_channel[cid]
    return None


def _describe(old, new):
    """One field's change, with empty rendered as a word rather than as nothing."""
    return f'{old or "(none)"} -> {new or "(none)"}'


def refresh_from_guide(recording_id: int):
    """Re-read this recording's program listing and update its metadata_* columns.

    Called from `recorder.start_recording()` once the recording's channel is settled and
    its status claim is won, and never from anywhere that could run concurrently with a
    user editing these fields.

    Three outcomes, all of them quiet about the ordinary case and loud about the rest:
    a value moved, so `RECORDING_METADATA_REFRESHED` names both sides of every change; the
    lock is set, so nothing is written and `RECORDING_METADATA_REFRESH_SKIPPED` says so;
    or the listing could not be found, which leaves the creation snapshot exactly as it
    was and logs why. A found-and-identical refresh writes nothing - an event on every
    recording saying that nothing happened is noise, and it would bury the two that matter.

    The lock FILTERS here, it is never cleared or "helpfully" overridden when a program
    changes a lot. That is the spiral CLAUDE.md's participation-switch rule exists to
    prevent, and this function is not a writer of that column.
    """
    rec = db.session.get(Recording, recording_id)
    if rec is None or rec.program_start_time is None:
        # A manual URL-only recording has no program to re-read, exactly as it carries no
        # program_title today. Not a failure and not worth a line.
        return

    if rec.metadata_locked:
        @retry_on_locked()
        def _log_skip_and_commit():
            add_recording_event(
                recording_id, RECORDING_METADATA_REFRESH_SKIPPED,
                detail='Program details are locked, so the record-start refresh left them '
                       'as they are.')
            db.session.commit()

        _log_skip_and_commit()
        return

    entry = find_program_entry(rec)
    if entry is None:
        log.info('Recording %d: no listing found for the program that was scheduled '
                 '(channel %s, program start %s) - keeping the details captured when it '
                 'was set up', recording_id, rec.channel_id, rec.program_start_time)
        return

    # Read the new values out of the ORM object BEFORE the write closure: a rollback
    # inside it expires every loaded row, so a retry would re-read this one from a session
    # that has thrown it away.
    incoming = {col: getattr(entry, attr) for col, attr, _ in _FIELDS}

    @retry_on_locked()
    def _refresh_and_commit():
        r = db.session.get(Recording, recording_id)
        # Re-checked inside the unit rather than trusted from above, because the lock is
        # the whole reason this function may not write, and a retry re-runs from here.
        if r is None or r.metadata_locked:
            return []
        moved = []
        for col, _attr, label in _FIELDS:
            new = incoming[col]
            old = getattr(r, col)
            # Never blind-assign: a listing that has lost a field must not wipe what was
            # captured when the recording was scheduled.
            if new is None or new == old:
                continue
            setattr(r, col, new)
            moved.append(f'{label} {_describe(old, new)}')
        if moved:
            add_recording_event(
                recording_id, RECORDING_METADATA_REFRESHED,
                detail='The program listing changed after this was scheduled: '
                       + '; '.join(moved),
                extra={'changed': [m.split(' ', 1)[0] for m in moved]})
            db.session.commit()
        return moved

    changed = _refresh_and_commit()
    if changed:
        log.info('Recording %d: program details refreshed at record start (%s)',
                 recording_id, ', '.join(changed))


# ---------------------------------------------------------------------------
# The user's own corrections
# ---------------------------------------------------------------------------

#: The columns the preview-and-correct surface may move, with the label the event and the
#: UI both use. `metadata_title` is here and `program_title` is deliberately not: that pair
#: is the immutable record of what we planned to record, and this is what the file says.
EDITABLE_FIELDS = (
    ('metadata_title', 'Title'),
    ('metadata_description', 'Synopsis'),
    ('metadata_category', 'Genre'),
    ('metadata_rating', 'Rating'),
)

#: Column widths, spelled once so the route's refusal and app/database.py cannot drift.
#: SQLite does not enforce a VARCHAR length, so a value longer than the column would be
#: stored happily here and truncated by any other database this ever runs on - which is
#: the kind of silent damage that shows up months later in somebody's library.
MAX_LENGTHS = {
    'metadata_title': 512,
    'metadata_description': 20000,
    'metadata_category': 255,
    'metadata_rating': 64,
}

#: Distinguishes "the user did not touch this" from "the user cleared this". A caller that
#: leaves a field out must not have it wiped, and None is already the meaningful value for
#: every one of these columns.
UNSET = object()


def validate_edit(values: dict):
    """The reason this edit must be refused, or None when it is fine.

    Called by the route BEFORE anything is mutated. The UI enforces the same limits in the
    form, and that enforcement is not the gate - CLAUDE.md's "enforcement lives server-side"
    rule means a payload that never went through the form gets the same answer.
    """
    for col, label in EDITABLE_FIELDS:
        value = values.get(col, UNSET)
        if value is UNSET or value is None:
            continue
        if not isinstance(value, str):
            return f'{label} must be text.'
        if len(value) > MAX_LENGTHS[col]:
            return (f'{label} is too long - {len(value)} characters, and the limit is '
                    f'{MAX_LENGTHS[col]}.')
    return None


def _clean(value):
    """A submitted field as it should be stored: blank becomes None, never ''.

    Every one of these columns already treats None as "nothing here", and the sidecar
    renderer omits an absent element rather than writing an empty one. Storing '' would
    give each column a second way to say the same thing, and the two would then have to be
    checked for separately at every reader.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _switch_words(value):
    return 'on' if value else 'off'


def apply_user_edit(rec, values=None, lock=UNSET, sidecar_enabled=UNSET):
    """Move a recording's own metadata to what the user typed, and log the move. No commit.

    THE one writer of `Recording.metadata_locked` and of the fields above, in the shape
    `channel_groups.set_participation()` established: the column and the event that explains
    it move together, in one function, so neither can happen without the other. The lock and
    the per-recording sidecar switch each store the user's answer to a judgment call, which
    is the class of column CLAUDE.md's participation-switch rule says no engine may write -
    and a switch that can move with nothing on any surface saying so is the exact defect
    that rule was written after (dev/changelog/1058).

    Deliberately does NOT commit. The caller owns that, so the whole read-modify-write stays
    inside one `retry_on_locked` unit; committing here would split it in two and a retry
    would replay only half.

    `lock` and `sidecar_enabled` default to UNSET rather than None because None is a real
    value for the second one ("inherit the profile"), and a caller that omits a field must
    never have it cleared.

    Returns the list of changes made, each already written as a sentence for a human. An
    empty list means the submission matched what was already stored, and nothing at all is
    written - an event on every Save saying that nothing changed is noise that would bury
    the ones that matter.
    """
    values = values or {}
    moved = []
    moved_cols = []

    for col, label in EDITABLE_FIELDS:
        submitted = values.get(col, UNSET)
        if submitted is UNSET:
            continue
        new = _clean(submitted)
        old = getattr(rec, col)
        if new == old:
            continue
        setattr(rec, col, new)
        moved.append(f'{label} {_describe(old, new)}')
        moved_cols.append(col)

    if lock is not UNSET:
        new_lock = bool(lock)
        if new_lock != bool(rec.metadata_locked):
            # This function IS the column's one writer, named in _CANONICAL over in
            # MetadataLockWriteBypassTests: the lock moves and the event naming the move is
            # written in the same breath, so the switch can never change with nothing on
            # any surface saying so.
            rec.metadata_locked = new_lock
            moved.append(
                'Lock turned ' + _switch_words(new_lock)
                + (' - the record-start refresh will leave these alone'
                   if new_lock else ' - the guide can update these again'))

    if sidecar_enabled is not UNSET:
        new_sidecar = None if sidecar_enabled is None else bool(sidecar_enabled)
        if new_sidecar is not rec.metadata_sidecar_enabled:
            rec.metadata_sidecar_enabled = new_sidecar
            moved.append(
                'Metadata file for this recording set to '
                + ('follow its profile' if new_sidecar is None
                   else _switch_words(new_sidecar)))

    if moved:
        add_recording_event(
            rec.id, RECORDING_METADATA_EDITED,
            detail='Edited by hand: ' + '; '.join(moved),
            # Only what actually moved. A submission that re-sent every field unchanged
            # would otherwise leave an event claiming four edits that never happened.
            extra={'fields': moved_cols})
    return moved
