"""The file a finished recording uses to describe itself to a media server.

A recording arrives in a Plex, Jellyfin, Emby or Kodi library as a bare video file, and
everything those servers can tell a user about it has to be guessed from the filename. This
module writes the answer down instead: a Kodi/XBMC-format `.nfo` beside the final file,
plus a poster image, both named after the video so no folder restructuring is needed. The
poster is the image pinned to the recording's profile when it has one
(app/profile_posters.py), otherwise the frame captured from inside the program itself
(recorder.persist_poster_frame), otherwise the final-frame thumbnail.

The format is the Kodi one because it is the portable one. Jellyfin, Emby and Kodi have
read it for years, and Plex reads it natively as of PMS 1.43.1 through its own "Plex NFO
Movie" / "Plex NFO Series" agent - no plugin, no token, and no network call, so a server
being down can never delay or damage a recording. That agent covers Movie and TV Show
libraries only; an "Other Videos" library stays on the Personal Media agent and will
ignore what is written here.

`<movie>` is the only shape phase 1 renders, deliberately. A live sports capture will
never match TheTVDB, so the point is not to help a server match - it is to remove matching
from the question, by making this file the authority for a self-contained item with a
title, a date, a summary and a poster. The `<episodedetails>` shape is a real feature and
not a rename: it needs season and episode numbers the EPG routinely does not carry, a
show-folder layout and a `tvshow.nfo` above it. `_RENDERERS` is where it lands when it is
built, so nothing here has to be restructured for it - but until then the app does not
offer a choice it cannot honor (dev/changelog/1057).

Off by default, behind a three-level gate, innermost first:
`Recording.metadata_sidecar_enabled`, then `RecordingProfile.metadata_sidecar_enabled`,
then the global `recording.metadata_sidecar.enabled`. Both overrides are tri-states where
None means "ask the level above". `sidecar_source()` answers which level decided, so a
surface can say why the answer is what it is rather than only what it is.

Nothing here may fail a recording. The capture is finished and on disk by the time this
runs, and a convenience that damages the artifact it describes is the thing the product
principles forbid outright; every failure becomes an event naming the reason and the
post-process chain carries on.
"""
import logging
import os
import shutil
import xml.etree.ElementTree as ET

from . import db
from .database import (
    Recording, add_recording_event,
    METADATA_SIDECAR_WRITTEN, METADATA_SIDECAR_FAILED,
)
from .db_utils import retry_on_locked
from .tz_utils import get_display_tz, to_local

log = logging.getLogger(__name__)

#: What the sidecar adds to a video's stem. `-poster` rather than a bare `.jpg` because
#: Kodi and Jellyfin both name a flat-folder poster that way and Plex accepts either, so
#: the suffix that works everywhere costs nothing over the one that works on Plex.
#: POSTER_SUFFIX is the captured frame's name; a poster pinned to the profile keeps its
#: own format, so a PNG lands as `-poster.png` rather than a PNG wearing `.jpg`
#: (dev/changelog/1059). Every reader of this layout accepts both.
NFO_SUFFIX = '.nfo'
POSTER_SUFFIX = '-poster.jpg'
POSTER_SUFFIXES = (POSTER_SUFFIX, '-poster.png')


def sidecar_paths(video_path: str) -> list:
    """The files this module could write beside `video_path`, whether or not they exist:
    the .nfo first, then every name a poster can take.

    The one place those names are spelled, so the writer and
    recorder.recording_disk_paths() (which deletes them) cannot disagree about what a
    recording owns. Deliberately NOT folded into postprocessor.output_extension_family():
    that family decides which stems a concat may CLAIM, and a leftover poster would then
    push a legitimately free stem to a `_2` suffix.
    """
    stem = os.path.splitext(video_path)[0]
    return [stem + NFO_SUFFIX] + [stem + suffix for suffix in POSTER_SUFFIXES]


def sidecar_source(cfg: dict, profile, recording=None):
    """Who decided whether this recording gets a sidecar, and what they decided.

    Returns `(level, enabled)` where level is 'recording', 'profile' or 'global'. The level
    exists so a surface can say WHERE an answer came from instead of only what it is: a
    switch that reads "Off" with no indication that the profile is what turned it off sends
    the user to change the global setting, which will not help.

    Innermost wins. Both overrides are tri-states, tested with `is not None` rather than
    truthiness, because an override that turned the feature OFF while the level above it is
    on is exactly the case a falsy test would silently drop.
    """
    if recording is not None and recording.metadata_sidecar_enabled is not None:
        return 'recording', bool(recording.metadata_sidecar_enabled)
    if profile is not None and profile.metadata_sidecar_enabled is not None:
        return 'profile', bool(profile.metadata_sidecar_enabled)
    return 'global', bool(
        cfg.get('recording', {}).get('metadata_sidecar', {}).get('enabled', False))


def sidecar_enabled(cfg: dict, profile, recording=None) -> bool:
    """Whether a sidecar should be written for this recording."""
    return sidecar_source(cfg, profile, recording)[1]


def globally_enabled(cfg: dict) -> bool:
    """The global setting alone, ignoring every override.

    This is the switch that decides whether the feature's UI exists at all, which is a
    different question from whether a given recording gets a file. A per-recording switch
    hidden because that same switch is off is a trap: turning it back on would need the
    control it just hid.
    """
    return bool(cfg.get('recording', {}).get('metadata_sidecar', {}).get('enabled', False))


def _display_title(rec) -> str:
    """What the library should call this item.

    A title the user typed wins outright - that is the whole point of being able to correct
    this, and provider EPG for sports is routinely a placeholder. Everything below it is the
    derivation used when they have not.

    The program's title alone is not it. Provider EPG titles are the name of the strand,
    not of the showing - every game in a season is "MLB Baseball" - so a library built on
    them is a wall of identical rows. The sub-title is the part that identifies the
    showing, so the two are joined when both exist.

    Falls back to the recording's own name, which is what a manual URL-only recording has
    and all it has: those carry no program_title, exactly as they carry no program_start_time.
    """
    return (rec.metadata_title or '').strip() or derived_title(rec)


def derived_title(rec) -> str:
    """The title this recording would carry if nobody had typed one.

    Split out from `_display_title()` so the edit surface can show what clearing the field
    falls back to. A blank box on that form means "derive it", and a user who cannot see
    what that derives to is being asked to clear a field on faith.
    """
    title = (rec.program_title or '').strip()
    sub_title = (rec.program_sub_title or '').strip()
    if title and sub_title:
        return f'{title} - {sub_title}'
    return title or (rec.name or '').strip()


def _air_date(rec, tz):
    """The program's air date as YYYY-MM-DD in the user's timezone, or None.

    LOCAL, not the stored UTC: a game that airs at 8pm Eastern is stored as the next day in
    UTC, and a library showing tomorrow's date on last night's recording is wrong in the
    only way a user would ever check. Anchored on program_start_time - the immutable
    snapshot of when the program aired - and not on when capture happened to begin, which
    is a different fact and moves with padding, a late start or a retry.
    """
    if rec.program_start_time is None:
        return None
    return to_local(rec.program_start_time, tz).strftime('%Y-%m-%d')


def _text(parent, tag, value):
    """Append <tag>value</tag> when there is a value, and nothing at all when there is not.

    An empty element is not the same as an absent one to every reader of this format: some
    treat it as "the field is known to be blank" and stop looking for the data elsewhere.
    Absent is the honest rendering of "this recording has no synopsis".
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    node = ET.SubElement(parent, tag)
    node.text = text
    return node


def _render_movie(rec, tz, poster_name):
    """The `<movie>` document for one recording."""
    root = ET.Element('movie')
    _text(root, 'title', _display_title(rec))
    _text(root, 'plot', rec.metadata_description)
    # No <tagline>. The sub-title is the obvious candidate for it and is already inside
    # <title>, so emitting both made a real recording read "NASCAR Cup Series - Enjoy
    # Illinois 300" with "Enjoy Illinois 300" printed again directly underneath it.
    _text(root, 'genre', rec.metadata_category)
    # <mpaa> is where every reader of this format looks for a content rating, whatever the
    # provider's own scheme happens to be.
    _text(root, 'mpaa', rec.metadata_rating)
    aired = _air_date(rec, tz)
    if aired:
        _text(root, 'premiered', aired)
        _text(root, 'year', aired[:4])
    # The feed this was captured from. <studio> is the closest thing the movie shape has to
    # "where it came from", and a user scanning a library of recordings wants to know.
    if rec.channel is not None:
        _text(root, 'studio', rec.channel.name)
    if poster_name:
        thumb = _text(root, 'thumb', poster_name)
        if thumb is not None:
            thumb.set('aspect', 'poster')
    return root


#: Root-element shape by NFO kind. One entry today; see the module docstring for why the
#: TV shape is a hole rather than a second entry that half works.
_RENDERERS = {
    'movie': _render_movie,
}

DEFAULT_KIND = 'movie'


def render_nfo(rec, tz, poster_name=None, kind: str = DEFAULT_KIND) -> str:
    """The sidecar's full XML text for one recording.

    `poster_name` is the poster's BASENAME, not a path: the reference has to resolve
    relative to the folder the video sits in, and an absolute path written into the file
    stops resolving the moment the library is mounted somewhere else (a container, another
    machine, a rename of the share).
    """
    root = _RENDERERS[kind](rec, tz, poster_name)
    ET.indent(root, '  ')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            + ET.tostring(root, encoding='unicode') + '\n')


#: Where a written poster came from, for the METADATA_SIDECAR_WRITTEN event.
POSTER_FROM_PROFILE = 'profile'
POSTER_FROM_FRAME = 'frame'
POSTER_FROM_THUMBNAIL = 'thumbnail'

#: How each source reads in the event's sentence.
POSTER_ORIGINS = {
    POSTER_FROM_PROFILE: 'the image pinned to its profile',
    POSTER_FROM_FRAME: 'a frame from inside the program',
    POSTER_FROM_THUMBNAIL: 'the final frame of the recording',
}


def _drop_stale_posters(stem: str, keep: str):
    """Remove any other poster name beside the video, so the folder never holds a
    `-poster.jpg` from an earlier write next to the `-poster.png` the .nfo now names. A
    server that picks artwork by filename would otherwise choose between them by luck."""
    for suffix in POSTER_SUFFIXES:
        stale = stem + suffix
        if stale == keep or not os.path.exists(stale):
            continue
        try:
            os.remove(stale)
        except OSError as exc:
            log.warning('Could not remove the previous poster %s: %s', stale, exc)


def _copy_poster(recording_id, cfg, video_path, profile):
    """Put a poster beside the video. Returns `(path written or None, source, note)`.

    Three sources, best first. The image pinned to the recording's profile wins outright
    when there is one, in its own format (dev/changelog/1059) - it is the only one the user
    chose. Below it is the poster frame taken from inside the program itself
    (recorder.persist_poster_frame, dev/changelog/1060). Last is the final-frame thumbnail,
    which is a reasonable "what did this end on" image and a poor cover - a ball game ends
    on a postgame graphic - and is kept as the floor because every recording made before
    the poster frame existed has only that one.

    `note` is a sentence for the event when something worth saying happened - the pinned
    file was gone from disk - so a recording whose cover is not the logo the user chose
    can say why. No image at all is not a failure: the live-thumbnail feature can be
    switched off, and a recording whose segments were all unreadable never got either
    image. The NFO simply carries no <thumb> then.
    """
    from .profile_posters import poster_path
    from .recorder import poster_frame_path
    from .storage_dirs import THUMBNAILS, image_dir

    stem = os.path.splitext(video_path)[0]
    note = None
    if profile is not None and profile.poster_file:
        try:
            pinned = poster_path(cfg, profile.poster_file)
        except ValueError:
            pinned = None
        if pinned and os.path.exists(pinned):
            dest = stem + '-poster' + os.path.splitext(pinned)[1]
            shutil.copyfile(pinned, dest)
            _drop_stale_posters(stem, dest)
            return dest, POSTER_FROM_PROFILE, None
        note = (f'The poster pinned to profile "{profile.name}" was missing from disk '
                f'({pinned or profile.poster_file}), so it could not be used')
        log.warning('Recording %d: %s', recording_id, note)

    captured = [(poster_frame_path(recording_id, cfg), POSTER_FROM_FRAME),
                (os.path.join(image_dir(cfg, THUMBNAILS), f'{recording_id}.jpg'),
                 POSTER_FROM_THUMBNAIL)]
    for src, source in captured:
        if not os.path.exists(src):
            continue
        dest = stem + POSTER_SUFFIX
        shutil.copyfile(src, dest)
        _drop_stale_posters(stem, dest)
        return dest, source, note
    return None, None, note


def write_sidecar(recording_id: int, video_path: str, cfg: dict, profile=None):
    """Write the `.nfo` and poster for a finished recording beside `video_path`.

    Called from the post-process chain AFTER the move, because collision_safe_dest() can
    rename the video when something already occupies its name and the sidecar has to carry
    the name the video actually ended up with. A sidecar written before the move would name
    a file that is no longer there, which is worse than none - a media server would read it
    onto the wrong item.

    Returns True when a sidecar was written. A gate that is off, or a recording that cannot
    be found, returns False quietly; anything that goes wrong while writing returns False
    loudly, with a METADATA_SIDECAR_FAILED event naming the reason.

    The file writes sit OUTSIDE every retry_on_locked closure below. Writing a file is a
    non-idempotent side effect, and a lock retry that replayed it would copy a poster twice
    and could leave a half-written .nfo behind the second time.
    """
    rec = db.session.get(Recording, recording_id)
    if rec is None:
        return False
    if profile is None:
        profile = rec.profile
    if not sidecar_enabled(cfg, profile, rec):
        return False

    nfo_path = sidecar_paths(video_path)[0]
    # Read every value off the row before anything is written: the commit below can roll
    # back and retry, which expires loaded rows, and re-reading a detached one mid-write
    # is how a sidecar ends up describing half of what it started from.
    tz = get_display_tz()
    title = _display_title(rec)

    try:
        poster_path, poster_source, poster_note = _copy_poster(
            recording_id, cfg, video_path, profile)
        xml = render_nfo(rec, tz,
                         poster_name=os.path.basename(poster_path) if poster_path else None)
        # Written last, so a poster copy that died partway never leaves an .nfo pointing at
        # a truncated image.
        with open(nfo_path, 'w', encoding='utf-8') as fh:
            fh.write(xml)
    except (OSError, shutil.Error) as exc:
        reason = str(exc)
        log.warning('Recording %d: could not write its metadata sidecar: %s',
                    recording_id, reason)

        @retry_on_locked()
        def _log_failure_and_commit():
            add_recording_event(
                recording_id, METADATA_SIDECAR_FAILED,
                detail=f'The metadata file for media servers could not be written to '
                       f'{nfo_path}: {reason}. The recording itself is complete and '
                       f'untouched.')
            db.session.commit()

        _log_failure_and_commit()
        return False

    detail = f'Wrote {os.path.basename(nfo_path)} describing "{title}"'
    if poster_path:
        origin = POSTER_ORIGINS.get(poster_source, 'an image from the recording')
        detail += f', with {os.path.basename(poster_path)} as its poster ({origin})'
    else:
        detail += '. No poster was available to copy, so the file carries no artwork'
    if poster_note:
        detail += '. ' + poster_note

    @retry_on_locked()
    def _log_written_and_commit():
        add_recording_event(recording_id, METADATA_SIDECAR_WRITTEN, detail=detail,
                            extra={'poster': bool(poster_path),
                                   'poster_source': poster_source})
        db.session.commit()

    _log_written_and_commit()
    log.info('Recording %d: metadata sidecar written to %s', recording_id, nfo_path)
    return True
