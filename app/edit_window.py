"""What an edit of a SCHEDULED recording re-pads from when its profile changes (dev/changelog/1652).

A recording still at the window ChannelBin gave it moves to the new profile's padding, as a new
recording's dialog pads its program (guide.js::applyProfilePadding); a window a person set is theirs
and a profile change never moves it. Which window is ChannelBin's is the fact each surface keeps: a
rule's RuleMatch.rec_start_time/rec_stop_time, a league page Record's sports_window_start/stop. Any
other recording made from a listing is ChannelBin's while its times are the listing padded by its
profile, the window the dialog gave it.

The unpadded window is the one the recording's follower pads: for a recording that follows its game,
the league's answer for its listing (app/sports_follow.py), else the program's times. Saving a profile
change moves the stored fact to the new profile's window, so a window saved at it stays ChannelBin's
and neither follower moves it again, and a window a person set beside the change reads as theirs.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import db
from .database import RULE_MATCH_SCHEDULED, RecordingProfile, RuleMatch
from .sports_follow import KIND_RECORD, KIND_RULE, edit_window as followed_window, same_minute

#: Made from a listing by the TV Guide, a search or a channel page: nothing stored says whose it is.
KIND_LISTING = 'listing'


@dataclass(frozen=True)
class EditWindow:
    kind: str
    owned: bool          # the recording's times are still the ones ChannelBin gave it
    start: datetime      # the unpadded window a profile's padding is added to
    stop: datetime
    from_game: bool      # the window is the game's time by its league's answer, not the listing's


def padding(profile_id) -> tuple:
    """(pre, post) a profile adds; no profile adds none, as in the dialog."""
    p = db.session.get(RecordingProfile, profile_id) if profile_id is not None else None
    if p is None:
        return timedelta(0), timedelta(0)
    return timedelta(minutes=p.pre_padding_minutes or 0), timedelta(minutes=p.post_padding_minutes or 0)


def padded(win: EditWindow, profile_id) -> tuple:
    pre, post = padding(profile_id)
    return win.start - pre, win.stop + post


def _ledger(rec_id) -> list:
    return RuleMatch.query.filter_by(recording_id=rec_id, action_taken=RULE_MATCH_SCHEDULED).all()


def read(rec, now=None):
    """The EditWindow for `rec`, or None when it has no listing to pad from (a recording made by hand
    or from an event channel's name keeps the times it was given)."""
    if rec.program_start_time is None or rec.program_stop_time is None:
        return None
    current = (rec.start_time, rec.stop_time)
    followed = followed_window(rec.id, now)
    if followed is not None:
        kind, owned, (start, stop) = followed
        return EditWindow(kind, owned, start, stop, True)
    listing = (rec.program_start_time, rec.program_stop_time)
    if rec.rule_id is not None and rec.sports_record_tsdb_id is None:
        owned = any((m.rec_start_time, m.rec_stop_time) == current for m in _ledger(rec.id))
        return EditWindow(KIND_RULE, owned, *listing, False)
    if rec.sports_window_start is not None:
        owned = (rec.sports_window_start, rec.sports_window_stop) == current
        return EditWindow(KIND_RECORD, owned, *listing, False)
    win = EditWindow(KIND_LISTING, False, *listing, False)
    start, stop = padded(win, rec.profile_id)
    owned = same_minute(start, rec.start_time) and same_minute(stop, rec.stop_time)
    return EditWindow(KIND_LISTING, owned, *listing, False)


@dataclass(frozen=True)
class Repad:
    """What a save that changes an owned recording's profile writes beside the edit."""
    kind: str
    window: tuple        # ChannelBin's window under the new profile: the saved times when they match it
    matched: bool        # the saved times are that window
    pre_minutes: int
    post_minutes: int
    profile_name: str
    from_game: bool


def repad_for_save(rec, profile_id, start, stop, now=None):
    """The Repad an edit of `rec` saving `profile_id` at (start, stop) carries, or None when the
    profile did not change or the window was not ChannelBin's before the edit."""
    if profile_id == rec.profile_id:
        return None
    win = read(rec, now)
    if win is None or not win.owned:
        return None
    want = padded(win, profile_id)
    matched = same_minute(want[0], start) and same_minute(want[1], stop)
    pre, post = padding(profile_id)
    p = db.session.get(RecordingProfile, profile_id) if profile_id is not None else None
    return Repad(win.kind, (start, stop) if matched else want, matched,
                 int(pre.total_seconds() // 60), int(post.total_seconds() // 60),
                 p.name if p is not None else None, win.from_game)


def apply(r, repad: Repad, old):
    """Inside the edit's write unit, before its commit: move the stored fact that says the window
    is ChannelBin's from `old` (the times before the edit) to the new profile's window. A fact
    no longer at `old` was moved since the edit was read and is left to whoever moved it."""
    if repad.kind == KIND_RULE:
        for m in _ledger(r.id):
            if (m.rec_start_time, m.rec_stop_time) == old:
                m.rec_start_time, m.rec_stop_time = repad.window
    elif repad.kind == KIND_RECORD:
        if (r.sports_window_start, r.sports_window_stop) == old:
            r.sports_window_start, r.sports_window_stop = repad.window


def answer(rec, now=None) -> dict:
    """The record dialog's edit-window payload (GET /api/recordings/<id>/edit-window)."""
    win = read(rec, now)
    if win is None:
        return {'owned': False, 'why': ('This recording was not made from a guide listing, so a '
                                        "profile's padding does not change its times.")}
    if not win.owned:
        return {'owned': False, 'why': ('Its start and stop times were set by hand, so a '
                                        "profile's padding does not change them.")}
    return {'owned': True, 'from_game': win.from_game,
            'start_time': win.start.strftime('%Y-%m-%dT%H:%M:%S'),
            'stop_time': win.stop.strftime('%Y-%m-%dT%H:%M:%S')}
