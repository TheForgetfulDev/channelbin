"""The file a finished recording uses to describe itself to a media server.

A recording arrives in a Plex, Jellyfin, Emby or Kodi library as a bare video file, and
everything those servers can tell a user about it has to be guessed from the filename. This
module writes the answer down instead: a Kodi/XBMC-format `.nfo` beside the final file,
plus a poster image, both named after the video so no folder restructuring is needed. The
poster is the image pinned to the recording's profile when it has one
(app/profile_posters.py), otherwise TheSportsDB's poster or thumb for the recording's event
(app/sports_images.py), otherwise the frame captured from inside the program itself
(recorder.persist_poster_frame), otherwise the final-frame thumbnail. TheSportsDB's landscape
thumb is also written as the background, `-fanart`, whatever the poster is. A profile can put
TheSportsDB's own poster ahead of its pinned one (`prefer_sports_poster`, dev/changelog/1505).

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

from sqlalchemy.exc import SQLAlchemyError

from . import db
from .database import (
    Recording, RecordingSports, add_recording_event,
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
#: The background. `-fanart` is the one name Plex's local assets, Kodi and Jellyfin all read as a
#: movie's background art (dev/changelog/1465); it keeps its image's own format like the poster.
FANART_SUFFIXES = ('-fanart.jpg', '-fanart.png')


def sidecar_paths(video_path: str) -> list:
    """The files this module could write beside `video_path`, whether or not they exist:
    the .nfo first, then every name a poster and a background can take.

    The one place those names are spelled, so the writer and
    recorder.recording_disk_paths() (which deletes them) cannot disagree about what a
    recording owns. Deliberately NOT folded into postprocessor.output_extension_family():
    that family decides which stems a concat may CLAIM, and a leftover poster would then
    push a legitimately free stem to a `_2` suffix.
    """
    stem = os.path.splitext(video_path)[0]
    return [stem + NFO_SUFFIX] + [stem + suffix for suffix in POSTER_SUFFIXES + FANART_SUFFIXES]


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
    only way a user would ever check. Anchored on program_start_time - the snapshot of when
    the program aired, which only auto-record retrack moves, and then to where the guide
    moved the program (dev/changelog/1188) - and not on when capture happened to begin,
    which is a different fact and moves with padding, a late start or a retry.
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


def _render_movie(rec, tz, poster_name, sports=None, fanart_name=None):
    """The `<movie>` document for one recording. `sports` is its RecordingSports link, already
    known not to be removed, or None."""
    from .sports_data import link_facts
    root = ET.Element('movie')
    _text(root, 'title', _display_title(rec))
    if sports is not None:
        # The movie shape has no field for a season, a round or a venue, so they reach a library
        # three ways: the league and season as a set (a collection in Kodi and Jellyfin), the
        # league as a tag, and one sentence under the synopsis, which every server shows. The
        # title stays the guide's, or the one typed: naming a file from the event is a separate
        # option (DESIGN-sports-data.md 11).
        uid = _text(root, 'uniqueid', sports.tsdb_event_id)
        uid.set('type', 'thesportsdb')
    plot = (rec.metadata_description or '').strip()
    if sports is not None:
        plot = '\n\n'.join(x for x in (plot, link_facts(sports)) if x)
    _text(root, 'plot', plot)
    # No <tagline>. The sub-title is the obvious candidate for it and is already inside
    # <title>, so emitting both made a real recording read "NASCAR Cup Series - Enjoy
    # Illinois 300" with "Enjoy Illinois 300" printed again directly underneath it.
    _text(root, 'genre', rec.metadata_category)
    if sports is not None:
        collection = ET.SubElement(root, 'set')
        _text(collection, 'name', ' '.join(x for x in (sports.league_name, sports.season) if x))
        _text(root, 'tag', sports.league_name)
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
    if fanart_name:
        _text(ET.SubElement(root, 'fanart'), 'thumb', fanart_name)
    return root


#: Root-element shape by NFO kind. One entry today; see the module docstring for why the
#: TV shape is a hole rather than a second entry that half works.
_RENDERERS = {
    'movie': _render_movie,
}

DEFAULT_KIND = 'movie'


def render_nfo(rec, tz, poster_name=None, kind: str = DEFAULT_KIND, sports=None, fanart_name=None) -> str:
    """The sidecar's full XML text for one recording.

    `poster_name` is the poster's BASENAME, not a path: the reference has to resolve
    relative to the folder the video sits in, and an absolute path written into the file
    stops resolving the moment the library is mounted somewhere else (a container, another
    machine, a rename of the share). `sports` is the recording's TheSportsDB link from
    `active_sports_link()`, or None.
    """
    root = _RENDERERS[kind](rec, tz, poster_name, sports, fanart_name)
    ET.indent(root, '  ')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            + ET.tostring(root, encoding='unicode') + '\n')


#: Where a written poster came from, for the METADATA_SIDECAR_WRITTEN event.
POSTER_FROM_PROFILE = 'profile'
POSTER_FROM_FRAME = 'frame'
POSTER_FROM_THUMBNAIL = 'thumbnail'
POSTER_FROM_SPORTS = 'thesportsdb'
POSTER_FROM_SPORTS_THUMB = 'thesportsdb_thumb'

#: How each source reads in the event's sentence.
POSTER_ORIGINS = {
    POSTER_FROM_PROFILE: 'the image pinned to its profile',
    POSTER_FROM_FRAME: 'a frame from inside the program',
    POSTER_FROM_THUMBNAIL: 'the final frame of the recording',
    POSTER_FROM_SPORTS: "TheSportsDB's poster for its event",
    POSTER_FROM_SPORTS_THUMB: "TheSportsDB's background for its event, which has no poster",
}


def _drop_stale_posters(stem: str, keep: str, suffixes=POSTER_SUFFIXES):
    """Remove any other poster name beside the video, so the folder never holds a
    `-poster.jpg` from an earlier write next to the `-poster.png` the .nfo now names. A
    server that picks artwork by filename would otherwise choose between them by luck.
    `keep` None removes them all."""
    for suffix in suffixes:
        stale = stem + suffix
        if stale == keep or not os.path.exists(stale):
            continue
        try:
            os.remove(stale)
        except OSError as exc:
            log.warning('Could not remove the previous poster %s: %s', stale, exc)


def active_sports_link(recording_id: int):
    """The recording's TheSportsDB link, or None when it has none or a person removed it. A
    snapshot taken in the app's own database, so a sidecar never waits on TheSportsDB."""
    link = db.session.get(RecordingSports, recording_id)
    return link if link is not None and link.removed_at is None else None


def _sports_notes(art: dict) -> list:
    """Why each of TheSportsDB's images is not used, for the event: an image a recording expected
    and did not get is worth a sentence, since the frame it falls back to is a poor cover."""
    from .sports_images import LABELS, OFF, READY
    if any(state == OFF for state, _, _ in art.values()):
        return ["Downloading TheSportsDB's posters and backgrounds is off in Settings, so neither was used"]
    notes = []
    for role, (state, _, reason) in art.items():
        if state != READY:
            why = f' ({reason.rstrip(".")})' if reason else ''
            notes.append(f"TheSportsDB's {LABELS[role].lower()} for its event had not been downloaded{why}")
    return notes


def _copy_poster(recording_id, cfg, video_path, profile, sports=None, art=None):
    """Put a poster beside the video. Returns `(path written or None, source, note)`.

    Four sources, best first (the profile's switch below can swap the first two). The image
    pinned to the recording's profile wins when there is one, in its own format
    (dev/changelog/1059) - it is the only one the user chose. Below it is TheSportsDB's poster
    for the recording's event, or its landscape thumb when the event has none (`art`, from sports_images.artwork(), dev/changelog/1465): designed
    artwork beats a frame that landed on whatever graphic was on screen. Below that is the poster
    frame taken from inside the program itself
    (recorder.persist_poster_frame, dev/changelog/1060). Last is the final-frame thumbnail,
    which is a reasonable "what did this end on" image and a poor cover - a ball game ends
    on a postgame graphic - and is kept as the floor because every recording made before
    the poster frame existed has only that one.

    A profile with `prefer_sports_poster` on moves TheSportsDB's own portrait poster above its
    pinned image (dev/changelog/1505). Only the poster: the landscape thumb standing in for a
    missing one stays below the pin, since a league with almost no posters (NHL: 7 of 572)
    would otherwise trade the user's logo for a squeezed landscape on nearly every game.

    `note` is a sentence for the event when something worth saying happened - the pinned
    file was gone from disk, or the profile asked for TheSportsDB's poster first and either
    got it or did not - so a recording whose cover is not the one the user expected can say
    why. No image at all is not a failure: the live-thumbnail feature can be
    switched off, and a recording whose segments were all unreadable never got either
    image. The NFO simply carries no <thumb> then.
    """
    from .profile_posters import poster_path
    from .recorder import poster_frame_path
    from .sports_images import POSTER, READY
    from .storage_dirs import THUMBNAILS, image_dir

    stem = os.path.splitext(video_path)[0]
    notes = []
    state, path, _ = (art or {}).get(POSTER, (None, None, None))
    sports_ready = state == READY

    def _copy_sports():
        """Copy TheSportsDB's image as the poster; a failed copy is a note and a fall through,
        since the downloaded copy, not the recording, is what failed."""
        dest = stem + '-poster' + os.path.splitext(path)[1]
        try:
            shutil.copyfile(path, dest)
        except OSError as exc:
            notes.append(f"TheSportsDB's image could not be copied ({exc.strerror or exc})")
            log.warning('Recording %d: %s', recording_id, notes[-1])
            return None
        _drop_stale_posters(stem, dest)
        return dest

    pinned = None
    if profile is not None and profile.poster_file:
        try:
            pinned = poster_path(cfg, profile.poster_file)
        except ValueError:
            pinned = None
        if not (pinned and os.path.exists(pinned)):
            notes.append(f'The poster pinned to profile "{profile.name}" was missing from disk '
                         f'({pinned or profile.poster_file}), so it could not be used')
            log.warning('Recording %d: %s', recording_id, notes[-1])
            pinned = None

    prefer = pinned is not None and bool(profile.prefer_sports_poster)
    if prefer and sports_ready and sports.poster_url:
        dest = _copy_sports()
        if dest:
            notes.append(f'Profile "{profile.name}" puts TheSportsDB\'s poster ahead of its pinned one')
            return dest, POSTER_FROM_SPORTS, '. '.join(notes)
    if pinned is not None:
        if prefer and sports is not None and not sports.poster_url:
            notes.append(f'TheSportsDB has no poster for this event, so the one pinned to profile '
                         f'"{profile.name}" was used')
        # A poster TheSportsDB has but this server has not downloaded is said by write_sidecar's
        # _sports_notes(), with its reason.
        dest = stem + '-poster' + os.path.splitext(pinned)[1]
        shutil.copyfile(pinned, dest)
        _drop_stale_posters(stem, dest)
        return dest, POSTER_FROM_PROFILE, '. '.join(notes) or None

    if sports_ready:
        dest = _copy_sports()
        if dest:
            return dest, (POSTER_FROM_SPORTS if sports.poster_url else POSTER_FROM_SPORTS_THUMB), \
                '. '.join(notes) or None

    note = '. '.join(notes) or None
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


def _copy_fanart(recording_id, video_path, art):
    """Put TheSportsDB's landscape thumb beside the video as its background, or remove one an
    earlier write left when there is none now. Returns `(path written or None, note)`: a background
    that cannot be copied is left out and said, never a reason to lose the whole file."""
    from .sports_images import BACKGROUND, READY
    stem = os.path.splitext(video_path)[0]
    state, path, _ = (art or {}).get(BACKGROUND, (None, None, None))
    if state == READY:
        dest = stem + '-fanart' + os.path.splitext(path)[1]
        try:
            shutil.copyfile(path, dest)
        except OSError as exc:
            note = f"TheSportsDB's background could not be copied ({exc.strerror or exc}), so it was left out"
            log.warning('Recording %d: %s', recording_id, note)
            return None, note
        _drop_stale_posters(stem, dest, FANART_SUFFIXES)
        return dest, None
    _drop_stale_posters(stem, None, FANART_SUFFIXES)
    return None, None


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
    sports = active_sports_link(recording_id)
    from .sports_images import artwork
    art_note = fanart_note = None
    try:
        art = artwork(cfg, sports)
    except SQLAlchemyError as exc:
        # Its images are a nicety: a database that stayed locked costs them, not the file.
        db.session.rollback()
        art, art_note = {}, "TheSportsDB's images could not be looked up, so none was used"
        log.warning('Recording %d: %s: %s', recording_id, art_note, exc)

    try:
        poster_path, poster_source, poster_note = _copy_poster(
            recording_id, cfg, video_path, profile, sports, art)
        fanart_path, fanart_note = _copy_fanart(recording_id, video_path, art)
        xml = render_nfo(rec, tz,
                         poster_name=os.path.basename(poster_path) if poster_path else None,
                         sports=sports,
                         fanart_name=os.path.basename(fanart_path) if fanart_path else None)
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
    if sports is not None:
        detail += f', with the {sports.league_name} event details from TheSportsDB'
    if poster_path:
        origin = POSTER_ORIGINS.get(poster_source, 'an image from the recording')
        detail += f', with {os.path.basename(poster_path)} as its poster ({origin})'
    else:
        detail += '. No poster was available to copy, so the file carries no artwork'
    if fanart_path:
        detail += f", and {os.path.basename(fanart_path)} as its background (TheSportsDB's for its event)"
    # A pinned image is the poster the user chose, so TheSportsDB's missing one is not news,
    # unless the profile asked for TheSportsDB's poster first and it has one.
    from .sports_images import POSTER
    wanted = bool(profile is not None and profile.prefer_sports_poster and sports is not None
                  and sports.poster_url)
    unused = {k: v for k, v in art.items()
              if not (k == POSTER and poster_source == POSTER_FROM_PROFILE and not wanted)}
    for note in [n for n in (poster_note, fanart_note, art_note) if n] + _sports_notes(unused):
        detail += '. ' + note

    @retry_on_locked()
    def _log_written_and_commit():
        add_recording_event(recording_id, METADATA_SIDECAR_WRITTEN, detail=detail,
                            extra={'poster': bool(poster_path),
                                   'poster_source': poster_source,
                                   'fanart': bool(fanart_path)})
        db.session.commit()

    _log_written_and_commit()
    log.info('Recording %d: metadata sidecar written to %s', recording_id, nfo_path)
    return True
