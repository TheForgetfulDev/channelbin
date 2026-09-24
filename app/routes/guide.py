import json
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, jsonify

from .. import db
from ..database import (Account, Channel, ChannelEvent, ChannelGroup, EPGEntry, EpgSource,
                        Recording, RecordingProfile, Tag, SavedSearch, UserPref,
                        CHANNEL_ADDED_TO_GUIDE, CHANNEL_REMOVED_FROM_GUIDE,
                        REC_STATUS_ABORTED, REC_STATUS_FAILED)
from ..accounts import (normalize_url, render_filename_template,
                        duplicates_within, lifecycle_states_for_channels,
                        repoint_candidates_for_channels, transfer_channel_state,
                        # Moved beside render_filename_template so the channel search's
                        # airing grain can render a suggested name too (changelog 412).
                        filename_tag_cleanup as _tag_cleanup_from_config,
                        effective_filename_template as _effective_filename_template,
                        tags_matching as _matched_tags)
from ..channel_groups import (effective_score, rank_members, member_channels,
                              guide_row_targets, member_eager_options,
                              DEFAULT_FAILING_STREAK_THRESHOLD)
from .. import channel_hiding
from ..config import load_config
from ..db_utils import retry_on_locked
from ..logo_cache import resolve_logo_url
# The three moved to app/recording_match.py so the channel search's airing grain can
# reach them without importing from a route module (dev/changelog/412). Aliased to
# their old private names rather than renamed at ~20 call sites in this file.
from ..recording_match import (build_rec_indexes as _build_rec_indexes,
                               candidate_recs as _candidate_recs,
                               match_recording as _match_recording)
from ..tz_utils import get_display_tz
from .channel_tests import _latest_tests_by_channel, ANY_JOB
from .channels import _guide_conflicts, _parse_requested_ids, _BadRequestedIds
from .channel_groups import _next_guide_sort_order

guide_bp = Blueprint('guide', __name__)

# ── TV Guide Layout popover (DESIGN.md 12.3) ─────────────────────────────────
# Server-side per-user state, like the Columns popover and the section pickers - never
# localStorage (DESIGN.md 3.11). Desktop and mobile keep SEPARATE keys on purpose: a
# phone's field set must not reshape the desktop grid (12.3 as amended 2026-07-20).
GUIDE_LAYOUT_PREF_KEY = 'guide_layout_desktop'
GUIDE_LAYOUT_MOBILE_PREF_KEY = 'guide_layout_mobile'

# The single declaration of every Layout default. The template renders its checkboxes from
# this dict and the same dict is handed to guide.js as its initial state, so a checkbox's
# rendered `checked` attribute cannot drift from its JS default the way two independent
# declarations of one fact always eventually do (12.3's explicit warning). Guarded by
# tests/test_guide_layout_defaults.py.
#
# `collapse_gaps` is absent here because its default is the user's configured
# display.guide_collapse_gaps - see _guide_layout_defaults().
GUIDE_LAYOUT_FIELD_DEFAULTS = {
    'show_failed': False,   # "Include failed channels"
    'rec_status': True,
    'subtitle': True,
    'description': True,
    'start_time': False,
    'end_time': False,
    'duration': False,
    'tag_dots': True,
    'ch_resolution': True,
    'ch_fps': True,
    'ch_bitrate': True,
    'ch_audio': True,
}

# Layout state that is NOT a desktop popover checkbox, so it is deliberately kept out of the
# dict above (which tests/test_guide_layout_defaults.py pins one-to-one against the rendered
# checkboxes). Sort persists on both breakpoints - desktop drives it from the channel-column
# header menu (12.4), mobile from the Layout sheet (13.6/13.7), but the stored fact is the
# same one. `ch_logo_only` and `field_order` are mobile-only controls (13.5/13.6); they are
# read on both keys because a per-breakpoint defaults dict would be a second partition to
# keep in step for no gain - desktop simply never writes them.
GUIDE_LAYOUT_EXTRA_DEFAULTS = {
    'sort_key': 'guide',
    'sort_reversed': False,
    'ch_logo_only': False,
}

# Mirrors guide.js's SORT_KEYS (12.4 as amended). Only used to reject a stored sort key that
# no longer exists - a pref naming a retired key would otherwise sort by nothing, silently.
GUIDE_SORT_KEYS = ('guide', 'name', 'health')

# 13.9: the mobile program cell's fields render in the user's own order, so the order is
# itself stored state rather than the fixed 12.5 sequence desktop uses. `title` is in the
# list because it is orderable even though it can never be switched off. `time` is the one
# composite - it is the start/end/duration line that the three Time booleans feed.
GUIDE_CELL_FIELDS = ['rec_status', 'title', 'subtitle', 'time', 'tag_dots']

# 13.9's "detailed" default preset, and its one hard rule: a description never renders in a
# mobile grid cell (it lives in the record modal the cell opens), so mobile's default is off
# and guide.js refuses to emit it at phone widths regardless of what the stored pref says.
GUIDE_LAYOUT_MOBILE_OVERRIDES = {
    'start_time': True,
    'description': False,
}


def _guide_layout_defaults(cfg, mobile=False):
    """Layout defaults, with collapse-gaps seeded from config.

    display.guide_collapse_gaps stays the *initial* default (it is a shipped Settings field
    and the guide's behaviour before Layout existed); once the user touches the Layout
    popover, the stored pref wins for every field including this one. One precedence rule,
    one live source of truth - the Settings help text says so.
    """
    defaults = dict(GUIDE_LAYOUT_FIELD_DEFAULTS)
    defaults.update(GUIDE_LAYOUT_EXTRA_DEFAULTS)
    defaults['field_order'] = list(GUIDE_CELL_FIELDS)
    defaults['collapse_gaps'] = bool(cfg.get('display', {}).get('guide_collapse_gaps', True))
    if mobile:
        defaults.update(GUIDE_LAYOUT_MOBILE_OVERRIDES)
    return defaults


def _coerce_layout_value(key, value, default):
    """One stored value validated against its default's type, or None to reject it.

    Every value used to be coerced with bool(), which was right while every Layout setting
    was a checkbox. Now that sort and the mobile field order live in the same dict, a blind
    bool() would silently turn `field_order` into True - so the type of the default is what
    decides how the stored value is read, and anything that does not fit is dropped rather
    than half-converted.
    """
    if key == 'field_order':
        if not isinstance(value, list):
            return None
        # Unknown names are dropped (a retired field) and missing ones appended in the
        # canonical order (a field added after this pref was written), so a stale stored
        # order can never hide a field from the sheet or render one nothing knows about.
        seen = [f for f in value if f in GUIDE_CELL_FIELDS]
        ordered = list(dict.fromkeys(seen))
        ordered.extend(f for f in GUIDE_CELL_FIELDS if f not in ordered)
        return ordered
    if key == 'sort_key':
        return value if value in GUIDE_SORT_KEYS else None
    if isinstance(default, bool):
        return bool(value)
    return None


def read_guide_layout(cfg, pref_key, mobile=False):
    """Stored Layout pref merged over the defaults, ignoring keys we don't know about.

    An unknown key in a stale pref must never reach the template (it would render a
    checkbox nothing reads), and a newly-added field must keep its default rather than
    coming back False just because the stored dict predates it.

    Public because the channel detail page's "What's On" card embeds the same grid and
    reads its own Layout through here, under its own pref keys (dev/changelog/349). This
    module owns the defaults and the merge rules for every caller; a second reader would
    be a second set of them.
    """
    layout = _guide_layout_defaults(cfg, mobile=mobile)
    pref = db.session.get(UserPref, pref_key)
    if pref and pref.value:
        try:
            stored = json.loads(pref.value)
        except ValueError:
            stored = None
        if isinstance(stored, dict):
            for key, value in stored.items():
                if key not in layout:
                    continue
                coerced = _coerce_layout_value(key, value, layout[key])
                if coerced is not None:
                    layout[key] = coerced
    if mobile:
        # Not a default but a rule (13.9) - a pref written before the rule existed, or by a
        # hand-edited row, must still never put a description in a phone-width cell.
        layout['description'] = False
    return layout


def _load_tags():
    return Tag.query.all()


def epg_source_names() -> dict:
    """{source_id: name} for every EPG source - one small query per request."""
    return dict(db.session.query(EpgSource.id, EpgSource.name))


def _program_dict(ch, entry, start, stop, *, stream_url, template, tag_cleanup, rec, all_tags,
                  tags_by_name, tz, source_names, display_name=None, group_id=None):
    """Serialize one guide/search program cell (guide grid + extended search).

    entry=None → dummy filler slot for a channel with no EPG data.
    rec is the already-matched overlapping Recording (or None) - matching stays at
    call sites because the candidate set is built differently per route.
    display_name/group_id: set for channel-group rows - ch is then the group's
    active member (real recording target), display_name the group's name.
    source_names is epg_source_names(), read once per request: the record dialog names the
    EPG source a showing came from (DESIGN-epg-sources.md §9.4).
    """
    name = display_name or ch.name
    title = (entry.title or name) if entry else name
    sub_title = (entry.sub_title or '') if entry else ''
    description = (entry.description or '') if entry else ''
    category = (entry.category or '') if entry else ''
    suggested = render_filename_template(template, {
        'start_time': start,
        'stop_time': stop,
        'title': title,
        'sub_title': sub_title,
        'description': description,
        'channel_name': name,
        'category': category,
    }, tag_cleanup=tag_cleanup, tags_by_name=tags_by_name, tz=tz)
    has_rec = bool(rec)
    return {
        'id': entry.id if entry else None,
        'title': title,
        'sub_title': sub_title,
        'description': description,
        'start_time': start.strftime('%Y-%m-%dT%H:%M:%S'),
        'stop_time': stop.strftime('%Y-%m-%dT%H:%M:%S'),
        'category': category,
        'rating': (entry.rating or '') if entry else '',
        'source_name': source_names.get(entry.source_id) if entry else None,
        'stream_url': stream_url,
        'suggested_name': suggested,
        'channel_name': name,
        'channel_id': ch.id,
        'group_id': group_id,
        'has_recording': has_rec,
        'recording_id': rec.id if has_rec else None,
        'recording_status': rec.status if has_rec else None,
        'recording_start_time': rec.start_time.strftime('%Y-%m-%dT%H:%M:%S') if has_rec else None,
        'recording_stop_time': rec.stop_time.strftime('%Y-%m-%dT%H:%M:%S') if has_rec else None,
        'recording_profile_id': rec.profile_id if has_rec else None,
        'is_dummy': entry is None,
        'matched_tags': _matched_tags(all_tags, entry.title, entry.sub_title, entry.description) if entry else [],
    }


def _guide_row_entries(streak_threshold=DEFAULT_FAILING_STREAK_THRESHOLD):
    """Ordered guide rows - see channel_groups.guide_row_targets(), which owns the
    definition so the automatic health check probes the same member this paints
    (dev/changelog/752).

    Batches the members' latest tests here rather than leaving the helper to do it,
    because rank_members' bitrate tie-break needs the map and app/channel_groups.py has
    no business reaching into the guide's own query layer for it. The extra
    in_guide-group query the helper then repeats is one constant statement per render,
    not per row."""
    groups = ChannelGroup.query.filter_by(in_guide=True).options(*member_eager_options()).all()
    # One batched fetch for every group's members, not one query per group in the loop.
    all_member_ids = [m.channel_id for g in groups for m in g.memberships]
    latest_by_channel = _latest_tests_by_channel(all_member_ids, for_job_id=ANY_JOB)
    return guide_row_targets(streak_threshold=streak_threshold,
                             latest_by_channel=latest_by_channel)


@guide_bp.route('/guide')
def guide():
    accounts = Account.query.order_by(Account.created_at).all()
    if not accounts:
        return render_template('guide_empty.html', reason='no_accounts')
    cfg = load_config()
    streak_threshold = cfg.get('channel_testing', {}).get(
        'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
    rows = []
    for kind, obj, active in _guide_row_entries(streak_threshold):
        if kind == 'channel':
            rows.append({
                'dom_id': obj.id, 'name': obj.name, 'logo_url': resolve_logo_url(obj),
                'account_color': obj.account.color, 'notes': obj.notes,
                'is_group': False, 'member_count': 0,
            })
        else:
            rows.append({
                'dom_id': f'g{obj.id}', 'group_id': obj.id, 'name': obj.name,
                'logo_url': resolve_logo_url(active),
                'account_color': active.account.color, 'notes': None,
                'is_group': True, 'member_count': len(obj.memberships),
            })
    if not rows:
        return render_template('guide_empty.html', reason='no_channels')
    profiles = RecordingProfile.query.order_by(RecordingProfile.name).all()
    tags = Tag.query.order_by(Tag.name).all()

    # One load_config() for the whole request - never per row (CLAUDE.md no-hidden-I/O).
    cfg = load_config()
    # The rendered span is the same window the EPG importer stores, so the guide can never
    # promise more days than it has data for. DESIGN.md 12.1 forbids a typed day count in any
    # visible string, so this value feeds every piece of copy naming the window length.
    window_days = max(1, int(cfg.get('sync', {}).get('epg_days_ahead', 3) or 3))

    # Both breakpoints' Layout state is rendered into the page, because which one applies is
    # a CSS media query the server cannot see (12.3 as amended: separate keys per
    # breakpoint). The desktop dict also renders the popover's checkboxes; mobile's Layout
    # is a JS-built sheet, so its dict only ever reaches guide.js.
    layout = read_guide_layout(cfg, GUIDE_LAYOUT_PREF_KEY)
    layout_mobile = read_guide_layout(cfg, GUIDE_LAYOUT_MOBILE_PREF_KEY, mobile=True)

    # Where "Search all..." goes now the Extended Search modal is retired
    # (dev/changelog/416). Built through SearchState.to_params() rather than spelled in the
    # template, so this search's parameters have one speller across every entry point.
    from .channels import airing_search_url

    return render_template('guide.html', rows=rows, accounts=accounts, profiles=profiles,
                           tags=tags, guide_window_days=window_days, guide_layout=layout,
                           guide_layout_pref_key=GUIDE_LAYOUT_PREF_KEY,
                           guide_layout_mobile=layout_mobile,
                           guide_layout_mobile_pref_key=GUIDE_LAYOUT_MOBILE_PREF_KEY,
                           airing_search_url=airing_search_url())


@guide_bp.route('/guide/channels')
def channel_browser():
    """Old URL - moved to the consolidated Channels hub."""
    return redirect(url_for('channels.channel_browser', **request.args))


@guide_bp.route('/guide/epg')
def epg_status():
    """Old URL - the EPG Browser page it used to open is retired (dev/changelog/631);
    the consolidated Channels hub is the closest surviving page."""
    return redirect(url_for('channels.channel_browser', **request.args))


@guide_bp.route('/api/guide/saved-searches')
def list_saved_searches():
    searches = SavedSearch.query.order_by(db.func.lower(SavedSearch.query_text)).all()
    return jsonify({'results': [{'id': s.id, 'query': s.query_text} for s in searches]})


@guide_bp.route('/api/guide/saved-searches', methods=['POST'])
@retry_on_locked()
def create_saved_search():
    query = (request.get_json(silent=True) or {}).get('query', '').strip()
    if len(query) < 2 or len(query) > 255:
        return jsonify({'error': 'Query must be between 2 and 255 characters'}), 400

    saved = SavedSearch.query.filter(db.func.lower(SavedSearch.query_text) == query.lower()).first()
    if saved is None:
        saved = SavedSearch(query_text=query)
        db.session.add(saved)
        db.session.commit()

    return jsonify({'success': True, 'id': saved.id, 'query': saved.query_text})


@guide_bp.route('/api/guide/saved-searches/<int:search_id>/delete', methods=['POST'])
@retry_on_locked()
def delete_saved_search(search_id):
    saved = db.session.get(SavedSearch, search_id)
    if saved is not None:
        db.session.delete(saved)
        db.session.commit()
    return jsonify({'success': True})


@guide_bp.route('/api/guide/channels/<int:channel_id>/add', methods=['POST'])
@retry_on_locked()
def add_channel_to_guide(channel_id):
    """Give THIS channel its own guide row, which is what the button says and now all it
    does. Group membership is irrelevant here: it used to redirect the write onto the
    member's group, so the guide gained a row nobody asked for while the channel the user
    clicked gained nothing (dev/changelog/751). A group's own row is added from the group."""
    channel = db.session.get(Channel, channel_id)
    if channel is None:
        return jsonify({'error': 'Channel not found'}), 404

    force = request.args.get('force') == '1' or bool((request.get_json(silent=True) or {}).get('force'))

    if not channel.in_guide:
        if not force:
            conflicts = _guide_conflicts(channel)
            if conflicts:
                return jsonify({
                    'success': False,
                    'duplicate_warning': True,
                    'channel_id': channel.id,
                    'channel_name': channel.name,
                    'conflicting_channels': [{'id': c.id, 'name': c.name} for c in conflicts],
                })
        # Channels and groups share one guide ordering space - the next slot must
        # clear both, or a channel lands on a group's sort position.
        channel.guide_sort_order = _next_guide_sort_order()
        channel.in_guide = True
        # Same event the form path writes (routes/channels.py::toggle_channel). Gaining a
        # guide row from the search page used to leave no trace on the channel's own
        # timeline, so the two ways of doing one thing disagreed about whether it happened
        # (dev/changelog/792).
        db.session.add(ChannelEvent(
            channel_id=channel.id, event_type=CHANNEL_ADDED_TO_GUIDE,
            detail=f'Added to guide at position {channel.guide_sort_order}',
        ))
        # A guide row keeps a channel visible even when something says hide it, so gaining
        # one has to rewrite the answer - otherwise a hidden channel added to the guide
        # stays out of every picker that put it there.
        channel_hiding.recompute([channel.id])
        db.session.commit()

    return jsonify({'success': True, 'in_guide': True, 'channel_name': channel.name})


@guide_bp.route('/api/guide/channels/remove', methods=['POST'])
@retry_on_locked()
def remove_channels_from_guide():
    """Take each named channel's OWN guide row away - the exact inverse of
    `add_channel_to_guide`, and nothing wider. Group membership is untouched: a member
    whose group holds a row still has its listings on screen afterwards, which is why the
    two questions have separate spellings in the search vocabulary (`guiderow` vs
    `guidegroup`, dev/changelog/791).

    Deliberately not the `remove-duplicates` route, whose bare-`channel_ids` branch looks
    like this one: that one exists to carry the dedup transfer (guide listing, group
    membership, schedules and test enrollment move to the kept channel) and answers a
    different question. Bulk rather than per-channel because, unlike adding, there is no
    per-channel question to ask - one request is one transaction and one recompute.
    """
    try:
        ids = _parse_requested_ids((request.get_json(silent=True) or {}).get('channel_ids'))
    except _BadRequestedIds as err:
        return jsonify({'error': str(err)}), 400
    if not ids:
        return jsonify({'error': 'Select some channels first.'}), 400

    # Re-fetched inside the retried unit, per the retry rule: a rolled-back session expires
    # every pending change, so rows read outside would commit an empty transaction and
    # report success.
    removed = []
    for ch in Channel.query.filter(Channel.id.in_(ids)).all():
        # Already out of the guide is a no-op, not an error: the client's button is an
        # offer, never the gate, and a stale row payload must not be able to fail a
        # request the database says is already satisfied.
        if not ch.in_guide:
            continue
        db.session.add(ChannelEvent(
            channel_id=ch.id, event_type=CHANNEL_REMOVED_FROM_GUIDE,
            detail=f'Removed from guide (was position {ch.guide_sort_order})',
        ))
        ch.in_guide = False
        removed.append(ch.id)
    # A guide row DEFERS a hide rather than refusing it, so losing the row is what finally
    # lets a deferred hide take effect. Batched over the whole request rather than called
    # per channel - the helper takes a list and its pass is a single UPDATE.
    if removed:
        channel_hiding.recompute(removed)
    db.session.commit()
    return jsonify({'success': True, 'removed': removed})


@guide_bp.route('/api/guide/channels/remove-duplicates', methods=['POST'])
@retry_on_locked()
def remove_duplicate_channels_from_guide():
    """Bulk-remove losing duplicate channels from the TV Guide (sets in_guide=False),
    optionally transferring each removed channel's guide listing/group membership/
    scheduled recordings/health-check enrollment to the channel kept in its place
    (transfer_channel_state(), the same transfer Re-point uses)."""
    data = request.get_json(force=True, silent=True) or {}
    # `removals` (dedup path) carries a keep_channel_id per entry; a bare channel_ids
    # list is normalized into the same shape here.
    removals = data.get('removals')
    if removals is None:
        removals = [{'channel_id': cid} for cid in (data.get('channel_ids') or [])]
    if not removals:
        return jsonify({'error': 'No channels specified'}), 400
    transfer = bool(data.get('transfer'))

    cfg = load_config() if transfer else None
    removed = []
    transferred = []
    transfer_skipped = []
    for r in removals:
        cid = r.get('channel_id')
        ch = db.session.get(Channel, cid)
        if ch is None or not ch.in_guide:
            continue
        did_transfer = False
        if transfer:
            keep_id = r.get('keep_channel_id')
            keeper = db.session.get(Channel, keep_id) if keep_id else None
            # Server re-validates the keeper from DB ids - never trust the client blindly
            # (same "server re-validates everything" pattern as the repoint route).
            if keeper is None or keeper.id == ch.id:
                transfer_skipped.append({'channel_id': cid, 'reason': 'No valid channel to keep'})
            elif keeper.stream_url != ch.stream_url:
                transfer_skipped.append({'channel_id': cid,
                                         'reason': 'Kept channel no longer shares the stream URL'})
            else:
                transfer_channel_state(ch, keeper, cfg)
                transferred.append(cid)
                did_transfer = True
        if not did_transfer:
            ch.in_guide = False
            removed.append(cid)
    # Both halves change protection: a removal drops a guide row, and a transfer moves one
    # (plus its group memberships) from one channel to another. Every channel named in the
    # request is recomputed rather than only the removed ones, so the keeper is covered too.
    channel_hiding.recompute([r.get('channel_id') for r in removals if r.get('channel_id')]
                             + [r.get('keep_channel_id') for r in removals
                                if r.get('keep_channel_id')])
    db.session.commit()
    return jsonify({'success': True, 'removed': removed, 'transferred': transferred,
                    'transfer_skipped': transfer_skipped})


@guide_bp.route('/api/guide/epg')
def epg_api():
    start_str = request.args.get('start', '')
    end_str = request.args.get('end', '')
    try:
        window_start = datetime.fromisoformat(start_str.replace('Z', ''))
        window_end = datetime.fromisoformat(end_str.replace('Z', ''))
    except (ValueError, AttributeError):
        return jsonify({'error': 'Invalid start/end parameters'}), 400

    cfg = load_config()
    tag_cleanup = _tag_cleanup_from_config(cfg)
    streak_threshold = cfg.get('channel_testing', {}).get(
        'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
    # Hoisted beside cfg, not read inside the per-program loop below - _program_dict runs
    # once per cell and get_display_tz() is a load_config() (dev/docs/BUGS.md 2026-08-05).
    tz = get_display_tz()

    channel_id_param = request.args.get('channel_id', type=int)
    if channel_id_param:
        # Single-channel view (e.g. the channel detail page) - works regardless of
        # in_guide/grouping, since a channel can have EPG data without being in the guide.
        row_entries = [('channel', ch, None) for ch in Channel.query.filter_by(id=channel_id_param).all()]
    else:
        row_entries = _guide_row_entries(streak_threshold)

    # Load all recordings in the time window for has_recording check
    window_recordings = Recording.query.filter(
        Recording.start_time < window_end,
        Recording.stop_time > window_start,
        Recording.status.notin_([REC_STATUS_ABORTED, REC_STATUS_FAILED]),
    ).all()

    # group_id/channel_id/URL indexes (a channel can have more than one recording in
    # the window). Group-backed recordings key off group_id because failover can rewrite
    # their url mid-recording; channel-backed ones off channel_id because provider URL
    # drift can rewrite the channel's url out from under a frozen rec.url.
    rec_indexes = _build_rec_indexes(window_recordings)

    # ch = the row's recording target (a group row's current best member). Also pull in
    # every OTHER member of each group so the EPG-fallback and members-list ranking below
    # (rank_members) has bitrate data for the whole group, not just its active member.
    target_ids = [ch.id for _, obj, active in row_entries
                  for ch in [active if active is not None else obj]]
    other_member_ids = [m.channel_id for kind, obj, _ in row_entries if kind == 'group'
                        for m in obj.memberships]
    latest_test_by_channel = _latest_tests_by_channel(target_ids + other_member_ids, for_job_id=ANY_JOB)
    dup_titles = duplicates_within([obj for kind, obj, _ in row_entries if kind == 'channel'])
    # 'missing' surfaces provider-removed channels in the guide's own channel column
    # (dev/changelog/626) - the same derived state /channels and the EPG deep search already
    # show. Scoped to individual channel rows only, same as dup_titles above: a group's
    # lifecycle isn't a single channel's to report.
    lifecycle = lifecycle_states_for_channels(
        [obj for kind, obj, _ in row_entries if kind == 'channel'], cfg)
    # Whether a 'missing' channel above also has a Re-point recovery target (the same
    # detection the channel detail page's Re-point action uses) - lets the guide's tooltip
    # say so accurately instead of guessing from a cheaper, guide-scoped signal like
    # dup_titles above (which only sees in_guide channels and would miss a survivor that
    # isn't itself in the guide).
    repoint_candidates = repoint_candidates_for_channels(
        [obj for kind, obj, _ in row_entries if kind == 'channel'], cfg, lifecycle)
    all_tags = _load_tags()
    # Hoisted beside all_tags rather than derived per cell: the grid renders thousands of
    # program cells and render_filename_template would otherwise issue its own Tag query
    # for each one whenever a tag-cleanup list is set (dev/changelog/441).
    tags_by_name = {t.name: t for t in all_tags}
    source_names = epg_source_names()

    # One windowed query for every channel the page might need entries for (target_ids
    # plus every other group member the fallback loop below may probe), grouped in
    # Python - not one EPGEntry query per row (dev/changelog/567, BUGS.md 2026-08-11).
    epg_ids = target_ids + other_member_ids
    window_entries = EPGEntry.query.filter(
        EPGEntry.channel_id.in_(epg_ids),
        EPGEntry.start_time < window_end,
        EPGEntry.stop_time > window_start,
    ).order_by(EPGEntry.channel_id, EPGEntry.start_time).all()
    entries_by_channel = {}
    for entry in window_entries:
        entries_by_channel.setdefault(entry.channel_id, []).append(entry)

    def _entries_in_window(channel_id):
        return entries_by_channel.get(channel_id, [])

    result_channels = []
    for kind, obj, active in row_entries:
        group = obj if kind == 'group' else None
        ch = active if group is not None else obj
        display_name = group.name if group is not None else None

        template = _effective_filename_template(cfg, ch)
        entries = _entries_in_window(ch.id)
        if group is not None and not entries:
            # Feeds differ in EPG coverage - fall back to the best-scored member
            # that has data in this window (display only; ch stays the record target).
            for member in rank_members(member_channels(group.memberships), latest_test_by_channel,
                                       streak_threshold=streak_threshold):
                if member.id == ch.id:
                    continue
                entries = _entries_in_window(member.id)
                if entries:
                    break

        stream_url = normalize_url(ch.stream_url, ch.account, cfg)
        channel_recs = _candidate_recs(rec_indexes, ch, stream_url, group)

        programs = []

        if not entries:
            # No EPG data - generate 1-hour dummy slots so the guide stays usable
            slot = window_start.replace(minute=0, second=0, microsecond=0)
            while slot < window_end:
                slot_end = slot + timedelta(hours=1)
                rec = _match_recording(channel_recs, slot, slot_end)
                programs.append(_program_dict(
                    ch, None, slot, slot_end,
                    stream_url=stream_url, template=template,
                    tag_cleanup=tag_cleanup, rec=rec, all_tags=all_tags,
                    tags_by_name=tags_by_name, tz=tz, source_names=source_names,
                    display_name=display_name,
                    group_id=group.id if group is not None else None,
                ))
                slot = slot_end
        else:
            for entry in entries:
                rec = _match_recording(channel_recs, entry.start_time, entry.stop_time)
                programs.append(_program_dict(
                    ch, entry, entry.start_time, entry.stop_time,
                    stream_url=stream_url, template=template,
                    tag_cleanup=tag_cleanup, rec=rec, all_tags=all_tags,
                    tags_by_name=tags_by_name, tz=tz, source_names=source_names,
                    display_name=display_name,
                    group_id=group.id if group is not None else None,
                ))

        lt = latest_test_by_channel.get(ch.id)
        lifecycle_state, lifecycle_since = lifecycle.get(ch.id, (None, None)) if group is None else (None, None)
        seen_rec_ids = set()
        channel_rec_dicts = []
        for r in channel_recs:
            if r.id in seen_rec_ids:
                continue
            seen_rec_ids.add(r.id)
            channel_rec_dicts.append({
                'id': r.id,
                'start_time': r.start_time.strftime('%Y-%m-%dT%H:%M:%S'),
                'stop_time': r.stop_time.strftime('%Y-%m-%dT%H:%M:%S'),
                'status': r.status,
            })
        row = {
            # Group rows use a synthetic 'g<id>' - matches the data-channel-id the
            # template rendered, and stays stable if the active member shifts
            # between refreshes. Programs carry the real channel_id for recording.
            'id': f'g{group.id}' if group is not None else ch.id,
            'name': display_name or ch.name,
            'logo_url': resolve_logo_url(ch),
            'account_color': ch.account.color,
            # The mobile channel sheet names the owning account (13.10) - the account edge
            # colour alone is only readable against the status-bar legend, which a sheet
            # covers. Free: ch.account is already loaded for the colour on the line above.
            'account_name': ch.account.name,
            'default_profile_id': ch.default_profile_id,
            'programs': programs,
            'recordings': channel_rec_dicts,
            'last_test_status': lt.status if lt else None,
            'last_test_at': lt.test_started_at.strftime('%Y-%m-%dT%H:%M:%S') if lt else None,
            'last_test_resolution': lt.resolution if lt else None,
            'last_test_fps': lt.fps if lt else None,
            'last_test_bitrate_kbps': lt.bitrate_kbps if lt else None,
            # Audio half of the channel column's tech readout (DESIGN.md 12.4)
            'last_test_audio_codec': lt.audio_codec if lt else None,
            'last_test_audio_channels': lt.audio_channels if lt else None,
            'last_test_drop_count': lt.drop_count if lt else None,
            'last_test_error_detail': lt.error_detail if lt else None,
            'health_score': ch.health_score,
            'manual_health_adjustment': ch.manual_health_adjustment,
            'health_score_sample_count': ch.health_score_sample_count,
            'duplicate_title': dup_titles.get(ch.id) if group is None else None,
            'lifecycle': lifecycle_state,
            'lifecycle_date': lifecycle_since.strftime('%Y-%m-%d') if lifecycle_since else '',
            'lifecycle_repoint_available': group is None and ch.id in repoint_candidates,
        }
        if group is not None:
            row.update({
                'is_group': True,
                'group_id': group.id,
                'member_count': len(group.memberships),
                'active_channel_id': ch.id,
                'active_channel_name': ch.name,
                'members': [
                    {'name': m.name, 'account_name': m.account.name,
                     'effective_score': effective_score(m),
                     'scored': m.health_score is not None}
                    for m in rank_members(member_channels(group.memberships), latest_test_by_channel,
                                          streak_threshold=streak_threshold)
                ],
            })
        result_channels.append(row)

    return jsonify({
        'window_start': window_start.strftime('%Y-%m-%dT%H:%M:%S'),
        'window_end': window_end.strftime('%Y-%m-%dT%H:%M:%S'),
        'channels': result_channels,
    })
