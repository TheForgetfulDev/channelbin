import json
import logging
import threading
from datetime import datetime
from urllib.parse import parse_qsl, urlencode

from flask import (Blueprint, render_template, request, redirect, url_for, flash, jsonify,
                   abort, current_app, send_from_directory)
from werkzeug.datastructures import MultiDict

from .. import db
from ..database import (
    Account, Channel, ChannelGroup, ChannelGroupMember, EPGEntry, Recording,
    ChannelTest, ChannelEvent, OnDemandTestJob, Tag,
    RecordingProfile, HealthCheckProfile,
    CHANNEL_ADDED_TO_GUIDE, CHANNEL_REMOVED_FROM_GUIDE, CHANNEL_HEALTH_OVERRIDE_CHANGED,
    REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING,
    REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING, REC_STATUS_COMPLETED,
    REC_STATUS_FAILED, REC_STATUS_ABORTED,
)
from ..accounts import (
    duplicate_groups_within, lifecycle_states_for_channels,
    NORM_DISABLED, resolve_normalization_mode, url_is_normalizable,
    missing_channels_query, next_sync_map, repoint_candidates_for_channels,
    _recompute_duplicate_stream_urls, transfer_channel_state,
)
from ..channel_groups import group_channel_ids, report_orphaned_guide_groups
from .. import channel_hiding
from ..config import load_config
from ..db_utils import retry_on_locked
from ..logo_cache import get_logo_cache_dir, resolve_logo_url
from .. import fmt_utils

log = logging.getLogger(__name__)

# Recording statuses that must block deleting the channel they reference outright -
# an in-flight capture/concatenation still needs the channel row. Terminal statuses
# (COMPLETED/FAILED/ABORTED) instead get channel_id set to NULL so the recording keeps
# its own name-only historical record (recordings.py already renders rec.channel is
# None as "Unknown channel" everywhere - this is a supported path, not new territory).
ACTIVE_RECORDING_STATUSES = (REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED,
                             REC_STATUS_RETRYING, REC_STATUS_CONCATENATING,
                             REC_STATUS_ANALYZING)
TERMINAL_RECORDING_STATUSES = (REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED)

# Bulk SQL IN(...) chunk size for the missing-channel delete - keeps each statement well
# under any SQLITE_MAX_VARIABLE_NUMBER a deployment might be compiled with (this build
# tolerates far more, but the Docker/other-platform builds that public packaging will add
# may not).
_DELETE_CHUNK_SIZE = 500

# Ceiling on an explicitly-requested delete set (the detail page's single channel, the
# Browse tab's selection). The teardown itself chunks and would survive far more; this
# bounds what one request can make the server hydrate and partition, since unlike the
# account and group scopes the id list arrives from the client (dev/changelog/772).
_MAX_REQUESTED_DELETE_IDS = 5000

# The two /api/user-prefs keys the channel search owns. Named here rather than spelled at
# each use site because the route writes them into the page and the page writes them back:
# a typo in one of the two copies would silently store a preference nothing ever reads.
#
# THE `_v2` SUFFIX IS A RESET, and it is what a stored setup gets when the registry it
# describes changes incompatibly rather than just gaining a column. The page tolerates a
# stored key it no longer knows (it filters unknown keys and appends new ones), which is
# right for an ADDED column and wrong for this one: the Status column was deleted and the
# default set was rebuilt, so merging a stored setup forward would leave anyone who had
# ever opened the Columns popover on a set nobody chose - the old four minus Status, with
# Groups arriving switched off because the old `hidden` list said so (dev/changelog/860).
# The v1 rows are left in place, unread and harmless. Bump the suffix again only for
# another incompatible change; a new column never needs one.
CHANNEL_SEARCH_COLUMNS_PREF = 'channel_search_columns_v2'
CHANNEL_SEARCH_SAVED_PREF = 'channel_search_saved'
# Which card LINES the phone arrangement draws. Deliberately NOT the columns row: the
# desktop setup is an order plus a hidden set over table tracks, this is a visibility set
# over card lines, and one row would make each width overwrite the other's choice.
CHANNEL_SEARCH_CARD_FIELDS_PREF = 'channel_search_card_fields_v2'
# The AIRING grain's own two, and they are separate rows rather than a nested object
# inside the channel grain's for the same reason the card fields are separate from the
# columns: two grains legitimately want different setups, and one shared row would have
# each overwrite the other every time the grain toggle was flipped. Separate rows also
# mean a setup stored before the airing grain existed still loads unchanged.
CHANNEL_SEARCH_AIRING_COLUMNS_PREF = 'channel_search_airing_columns_v2'
CHANNEL_SEARCH_AIRING_CARD_FIELDS_PREF = 'channel_search_airing_card_fields_v2'
# Which version of the group-create flow's intro screen has been dismissed. A VERSION, not
# a boolean, so a release that changes what the screen says can bump the constant in
# static/js/group-create-flow.js and show it once more (dev/changelog/831). Spelled here
# and in that file, and the two must agree - the JS posts it back to this key.
GROUP_INTRO_SEEN_PREF = 'group_intro_seen_version'


def _chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]
from .channel_tests import _serialize_dup_groups
from .channel_groups import _next_guide_sort_order
from ..channel_tester import get_status, health_check_profile_payload

channels_bp = Blueprint('channels', __name__)


def _guide_conflicts(channel):
    """Other channels sharing this channel's stream_url that are already in the guide."""
    return (
        Channel.query
        .filter(
            Channel.stream_url == channel.stream_url,
            Channel.id != channel.id,
            Channel.in_guide == True,
        )
        .all()
    )


#: Batched per-channel lifecycle states (DESIGN-sync-resilience.md §5). Moved to
#: app/accounts.py beside channel_lifecycle_state itself when the channel search's row
#: payload became its third caller (dev/changelog/398); kept under the old local name so the
#: call sites below still read as they did.
_lifecycle_states_for_channels = lifecycle_states_for_channels

#: The duplicate-repoint survivor lookup (DESIGN-sync-resilience.md §6). Moved to
#: app/accounts.py beside lifecycle_states_for_channels when the TV Guide's channel column
#: became its second caller (dev/changelog/627); kept under the old local name so the call
#: sites below and tests/test_sync_repoint.py's import still read as they did.
_repoint_candidates_for_channels = repoint_candidates_for_channels


def _unnormalizable_channel_ids(channels, cfg) -> set:
    """Ids of channels whose account has a normalization mode selected but whose URL has no
    user/password/id triplet to rebuild from, so normalization silently left it alone
    (changelog/258 Spec §2).

    Derived, never stored - it is a pure function of (url, mode) so it cannot go stale when
    either changes. The per-account mode is resolved once per account rather than per
    channel: resolve_normalization_mode() reads config when an account defers to the global
    default, which inside a row loop would be O(rows) disk I/O (CLAUDE.md "no hidden I/O in
    per-row loops").
    """
    mode_by_account: dict = {}
    out = set()
    for ch in channels:
        acct = ch.account
        if acct is None:
            continue
        if acct.id not in mode_by_account:
            mode_by_account[acct.id] = resolve_normalization_mode(acct, cfg)
        if mode_by_account[acct.id] == NORM_DISABLED:
            continue
        if not url_is_normalizable(ch.raw_stream_url or ch.stream_url or ''):
            out.add(ch.id)
    return out


def _missing_delete_candidates(account_id, cfg, group_id=None, channel_ids=None):
    """(eligible, blocked_inuse, blocked_active) - the shared partitioning for both the
    missing-channel bulk-delete preview and the delete action itself, so the two can never
    disagree about which channels are actually eligible (same shape as
    _repoint_candidates_for_channels / repoint_channel's server-side recompute).

    blocked_inuse: in_guide, or holds any ChannelGroupMember row (either kind) - excluded
    from the batch entirely rather than warned-and-allowed, since silently dropping
    something visibly in the live guide or a group is the riskier of the two options the
    spec left open (dev/changelog/248).
    blocked_active: referenced by a non-terminal Recording - deleting the channel out from
    under an in-flight capture/concatenation must never happen. REC_STATUS_SCHEDULED is in
    that set, so a channel with a recording scheduled against it blocks rather than
    cascading - the deliberate answer to "block, cascade or warn" (dev/changelog/772).

    `group_id` and `channel_ids` may combine: `group_id` scopes the candidate set to that
    group/health check's own channels and exempts THIS group's own membership rows from
    the "grouped" block, since by construction every candidate here is a member of it
    (membership in guide or in ANOTHER group still blocks, unchanged); `channel_ids`
    alongside it narrows that set further to an explicit subset - the group/health-check
    detail page's own selection-delete, scoped to just the channels selected there rather
    than the whole group. Without a `group_id`, `channel_ids` carries no such exemption -
    the single-channel delete on the channel detail page and the selection delete on the
    Browse tab (dev/changelog/772), neither of which has a "current group" to exempt, so a
    channel that is also in some group still blocks there. With neither, `account_id`
    applies (or no scope at all).

    Nothing about the request is trusted beyond the ids themselves: whether each one is
    actually missing from its provider's feed, and whether it is blocked, is re-derived
    here from the database on every call. An id that is neither eligible nor blocked is
    simply absent from all three lists - it is not missing (or no longer exists), which
    each caller reports rather than passing over in silence.
    """
    if group_id is not None:
        group = db.session.get(ChannelGroup, group_id)
        if group is None:
            return [], [], []
        scoped_ids = group_channel_ids(group)
        if channel_ids is not None:
            wanted = set(channel_ids)
            scoped_ids = [cid for cid in scoped_ids if cid in wanted]
        channel_ids = scoped_ids
        account_id = None
    elif channel_ids is not None:
        if not channel_ids:
            return [], [], []
        account_id = None

    candidates = missing_channels_query(account_id, cfg, channel_ids=channel_ids).all()
    if not candidates:
        return [], [], []

    candidate_ids = [ch.id for ch in candidates]
    grouped_ids_q = (
        db.session.query(ChannelGroupMember.channel_id)
        .filter(ChannelGroupMember.channel_id.in_(candidate_ids))
    )
    if group_id is not None:
        grouped_ids_q = grouped_ids_q.filter(ChannelGroupMember.group_id != group_id)
    grouped_ids = {r[0] for r in grouped_ids_q.all()}
    active_rec_ids = {
        r[0] for r in db.session.query(Recording.channel_id)
        .filter(Recording.channel_id.in_(candidate_ids))
        .filter(Recording.status.in_(ACTIVE_RECORDING_STATUSES)).all()
    }

    blocked_inuse, blocked_active, eligible = [], [], []
    for ch in candidates:
        if ch.id in active_rec_ids:
            blocked_active.append(ch)
        elif ch.in_guide or ch.id in grouped_ids:
            blocked_inuse.append(ch)
        else:
            eligible.append(ch)
    return eligible, blocked_inuse, blocked_active


# ── Browse tab: the channel search ─────────────────────────────────────────────
#
# The page renders its own results from `/api/channels/search` - the engine, the URL
# contract and the response envelope are dev/docs/DESIGN-channel-search.md. Everything
# here is therefore page chrome that does NOT depend on the search: what the shell needs
# to paint before the first fetch answers, plus the bulk-action modals this tab has always
# shipped. Nothing in this route filters channels; adding a filter here would be a second
# place that answers "which of my channels", which is the thing the engine exists to stop.

def _saved_searches():
    """The saved-search list as stored, or [] - never an exception.

    One /api/user-prefs row holds the whole list. It is user-written JSON, so a value that
    is not the list of records this expects is treated as "nothing saved" rather than being
    allowed to 500 the page it is only decoration on.
    """
    from ..database import UserPref

    pref = db.session.get(UserPref, CHANNEL_SEARCH_SAVED_PREF)
    try:
        rows = json.loads(pref.value) if pref and pref.value else []
    except ValueError:
        log.warning('Saved channel searches are not valid JSON - ignoring them.')
        return []
    return rows if isinstance(rows, list) else []


def _default_saved_search_params():
    """The query string of the saved search marked default, or '' when there is none.

    Read HERE, and turned into a redirect, rather than applied by the page's JS: applying it
    client-side would leave the address bar and the screen disagreeing about what is being
    shown, so a link copied out of the bar would not reproduce it.

    The stored spelling is re-parsed through SearchState and re-serialized rather than
    trusted. It is user text in a JSON blob, and a saved search naming a filter dimension
    that has since been removed must not turn every bare visit to this page into a 400 -
    it says so in the log and the page opens unfiltered.
    """
    from ..channel_search import SearchState, SearchStateError

    for row in _saved_searches():
        if not isinstance(row, dict) or not row.get('is_default'):
            continue
        try:
            state = SearchState.from_params(
                MultiDict(parse_qsl(row.get('params') or '', keep_blank_values=True)))
        except SearchStateError as exc:
            log.warning('Default saved search %r is not usable, opening unfiltered: %s',
                        row.get('name'), exc)
            return ''
        # Built from to_params(), never by re-emitting the stored text: that function is the
        # URL contract, so a link into this search has exactly one speller.
        return urlencode(state.to_params())
    return ''


def airing_search_url(query='', fields=None, replace_rec=None, **state_kwargs):
    """A link into the airing search, spelled ONCE.

    Every entry point into this search builds its URL from `SearchState.to_params()` rather
    than by hand-spelling parameter names in a template: the parameters are an API, so a
    renamed one is a breaking change to every link, and a link left unrepointed does not go
    red - it opens a search that quietly ignores what it was asked for
    (`DESIGN-channel-search.md` §2). Lives here because this module owns `channel_browser`,
    which is the page being linked to.
    """
    from ..channel_search import GRAIN_AIRINGS, SearchState

    if fields:
        state_kwargs['fields'] = tuple(fields)
    state = SearchState(q=query, grain=GRAIN_AIRINGS, replace_rec=replace_rec, **state_kwargs)
    params = urlencode(state.to_params())
    base = url_for('channels.channel_browser')
    return f'{base}?{params}' if params else base


def _replace_rec_context():
    """The recording a `replace_rec=` visit came here to replace, or None.

    Resolved server-side so the strip can name the recording rather than showing a bare id,
    and so the page never has to trust a display name handed to it in a URL - the same rule
    `add_to_group` follows (DESIGN-channel-search.md 2.4).

    **An id that no longer resolves drops the context with a log line rather than erroring.**
    A stale link is expected here: the recording may have started, finished or been deleted
    between the link being copied and being followed, and a decoration may not 500 the page it
    decorates. Only a SCHEDULED recording is replaceable - replacing one that is already
    running would mean deleting a capture in progress.
    """
    rec_id = request.args.get('replace_rec', type=int)
    if rec_id is None:
        return None
    rec = db.session.get(Recording, rec_id)
    if rec is None or rec.status != REC_STATUS_SCHEDULED:
        log.info('replace_rec=%s is not a scheduled recording any more - '
                 'opening the search without the replace context.', rec_id)
        return None
    return {'id': rec.id, 'name': rec.name}


def _channel_search_page_context():
    """Everything the search shell needs that is not part of the search itself.

    Deliberately small and search-independent: the row payload, the facet counts and the
    vocabularies all arrive over JSON, so nothing here may grow a per-row loop.
    """
    from ..database import UserPref

    cfg = load_config()
    # Global, not account-scoped: this page has no account selected until the user picks
    # one as an ordinary filter, and the button names a whole-database cleanup.
    missing_query = missing_channels_query(None, cfg)
    missing_count = missing_query.count()
    # Hidden channels stay eligible for this sweep (hidden means out of the way, not
    # protected), so the header names how many of the batch the user cannot currently see
    # in the search - otherwise the button would offer to delete rows nobody is looking at
    # with no way to tell.
    missing_hidden_count = missing_query.filter(Channel.hidden.is_(True)).count()

    # Review Duplicates is guide-scoped by design - it resolves channels already in the
    # guide that share a stream URL, which is a different question from the account-global
    # DUP badge a row carries. in_guide is true on a handful of rows, so this is cheap.
    guide_channels = Channel.query.filter_by(in_guide=True).all()
    dup_groups = _serialize_dup_groups(duplicate_groups_within(guide_channels), cfg)

    health_check_profiles = HealthCheckProfile.query.order_by(HealthCheckProfile.name).all()
    check_profiles = health_check_profile_payload(
        cfg.get('channel_testing', {}), health_check_profiles)

    # Column setup and saved searches are server-side user config so they follow the user
    # across browsers (DESIGN.md 3.11) - the same generic /api/user-prefs rows the
    # recordings list already uses. Rendered into the first paint rather than fetched, so
    # the table is not laid out twice.
    col_pref = db.session.get(UserPref, CHANNEL_SEARCH_COLUMNS_PREF)
    card_pref = db.session.get(UserPref, CHANNEL_SEARCH_CARD_FIELDS_PREF)
    airing_col_pref = db.session.get(UserPref, CHANNEL_SEARCH_AIRING_COLUMNS_PREF)
    airing_card_pref = db.session.get(UserPref, CHANNEL_SEARCH_AIRING_CARD_FIELDS_PREF)

    def _pref(row):
        return json.loads(row.value) if row and row.value else None

    # The health-check modal's "Run at" radio only renders when the schedule macro is
    # handed a window_label, so omitting it here removed maintenance-window mode from
    # this entry point entirely rather than erroring (dev/docs/BUGS.md 2026-08-27).
    from ..check_window import format_window_label

    # Which version of the group-create flow's intro screen this user has dismissed, in
    # the same generic user-prefs store the column setup uses so it follows them between
    # browsers. Server-rendered rather than fetched: the flow decides on its first screen
    # before it opens, and a fetch would decide it after the modal was already up.
    intro_pref = db.session.get(UserPref, GROUP_INTRO_SEEN_PREF)

    return {
        'window_label': format_window_label(cfg.get('channel_testing', {})),
        'group_intro_seen_version': _pref(intro_pref) or 0,
        'missing_count': missing_count,
        'missing_hidden_count': missing_hidden_count,
        'dup_groups': dup_groups,
        'check_profiles': check_profiles,
        'tester_status': get_status(),
        'col_prefs': _pref(col_pref),
        'card_field_prefs': _pref(card_pref),
        'airing_col_prefs': _pref(airing_col_pref),
        'airing_card_field_prefs': _pref(airing_card_pref),
        'saved_searches': _saved_searches(),
        'columns_pref_key': CHANNEL_SEARCH_COLUMNS_PREF,
        'card_fields_pref_key': CHANNEL_SEARCH_CARD_FIELDS_PREF,
        'airing_columns_pref_key': CHANNEL_SEARCH_AIRING_COLUMNS_PREF,
        'airing_card_fields_pref_key': CHANNEL_SEARCH_AIRING_CARD_FIELDS_PREF,
        'saved_pref_key': CHANNEL_SEARCH_SAVED_PREF,
        # The airing grain's Record button opens the shared record modal, which is
        # rendered by _record_modal.html and needs the profile list for its picker and
        # its padding maths. Four rows on this database - not a per-row cost.
        'profiles': RecordingProfile.query.order_by(RecordingProfile.name).all(),
        'replace_rec': _replace_rec_context(),
    }


@channels_bp.route('/channels')
def channel_browser():
    # A BARE visit only: any parameter at all means the visitor stated a search of their
    # own, and a default that overrode that could not be got rid of. A bare URL therefore
    # means "no search stated" and gets the default - including after Clear all, which
    # leaves the address bar bare. The page names the saved search it is showing next to
    # the Saved button, so that is disclosed rather than mysterious.
    if not request.args:
        params = _default_saved_search_params()
        # Non-empty by construction, so the redirected request carries parameters and
        # cannot come back through here: no loop to guard against beyond this.
        if params:
            return redirect(f'{request.path}?{params}')
    return render_template('channels/search.html', **_channel_search_page_context())


# ── Old Health Checks tab (superseded by the unified Groups tab, DESIGN.md §14) ──

@channels_bp.route('/channels/health-checks')
def channels_health_checks():
    """Old URL - health checks now live on the Groups tab (groups unification 4/4).
    There is no separate Health Checks page or nav link any more."""
    return redirect(url_for('channel_groups.groups_page'))


@channels_bp.route('/channels/health-checks/guide')
def channels_health_checks_guide():
    """Old URL - the guide run is now the TV Guide Channels system health check."""
    sys_job = OnDemandTestJob.query.filter_by(is_system=True).first()
    if sys_job is None:
        return redirect(url_for('channels.channels_health_checks'))
    return redirect(url_for('channels.health_check_detail', job_id=sys_job.id))


# ── Old URL redirects (Health / Test Runs merged into Health Checks) ────────

@channels_bp.route('/channels/health')
def channels_health():
    """Old URL - moved to the consolidated Health Checks list."""
    return redirect(url_for('channels.channels_health_checks_guide'))


@channels_bp.route('/channels/test-runs')
def channels_test_runs():
    """Old URL - moved to the consolidated Health Checks list."""
    return redirect(url_for('channels.channels_health_checks'))


@channels_bp.route('/channels/test-runs/<int:job_id>')
def test_run_detail(job_id):
    """Old URL - moved to the consolidated Health Checks list."""
    return redirect(url_for('channels.health_check_detail', job_id=job_id))


@channels_bp.route('/channels/health-checks/<int:job_id>')
def health_check_detail(job_id):
    """A health check IS its group's page. The URL is kept for bookmarks, alert links
    and the Jobs page, and redirects to `/channel-groups/<id>` - since every group
    carries exactly one check there is nothing for a check-shaped URL to pin
    (dev/changelog/1077; the shared page itself is dev/changelog/273)."""
    job = db.session.get(OnDemandTestJob, job_id)
    if job is None or job.group is None:
        abort(404)
    return redirect(url_for('channel_groups.group_detail', group_id=job.group_id))


# ── Channel detail (drill-down, not a tab) ──────────────────────────────────

CHANNEL_HEALTH_PAGE_SIZES = ('10', '25', '50', '100', 'all')


class _ListPagination:
    """Minimal page/pages/has_prev/has_next/prev_num/next_num shim for a plain Python
    list - mirrors the subset of flask_sqlalchemy.Pagination's public attributes
    detail.html already consumes, since Pagination itself isn't constructable
    outside .paginate()."""
    def __init__(self, page, per_page, total):
        self.page, self.per_page, self.total = page, per_page, total
        self.pages = max(1, -(-total // per_page)) if per_page else 1
        self.has_prev = page > 1
        self.has_next = page < self.pages
        self.prev_num = page - 1 if self.has_prev else None
        self.next_num = page + 1 if self.has_next else None


def _build_channel_timeline(channel_id):
    """Merge ChannelTest + Recording + ChannelEvent rows for one channel into a single
    chronological (newest-first) list of dicts, for the Activity Timeline section.

    Each entry carries `excluded`: True when the user has rolled the health score back past
    this observation (app/health_recompute.py), so the row can say it no longer counts. An
    excluded test still displays the "lifetime score after" it produced at the time, and
    that number no longer matches the channel's score - saying so is the point, since a row
    that silently disagrees with the score is the unexplainable number this app exists to
    avoid. One query for the whole set, not one per row.
    """
    from ..health_recompute import (excluded_keys, SOURCE_CAPTURE_CORRECTION,
                                    SOURCE_FAILOVER, SOURCE_FAST_DELIVERY, SOURCE_PLACEHOLDER,
                                    SOURCE_RECORDING, SOURCE_STALL_DEMOTION, SOURCE_TEST)
    from ..database import (CHANNEL_FAILOVER_HEALTH_OBSERVATION,
                            CHANNEL_PLACEHOLDER_HEALTH_OBSERVATION,
                            CHANNEL_FAST_DELIVERY_HEALTH_OBSERVATION,
                            CHANNEL_STALL_DEMOTION_HEALTH_OBSERVATION)

    excluded = excluded_keys(channel_id)
    event_source_kind = {
        CHANNEL_FAILOVER_HEALTH_OBSERVATION: SOURCE_FAILOVER,
        CHANNEL_STALL_DEMOTION_HEALTH_OBSERVATION: SOURCE_STALL_DEMOTION,
        CHANNEL_PLACEHOLDER_HEALTH_OBSERVATION: SOURCE_PLACEHOLDER,
        CHANNEL_FAST_DELIVERY_HEALTH_OBSERVATION: SOURCE_FAST_DELIVERY,
    }
    entries = []
    for t in ChannelTest.query.filter_by(channel_id=channel_id).all():
        entries.append({
            'kind': 'test', 'ts': t.test_started_at, 'obj': t,
            'quality_breakdown': json.loads(t.quality_breakdown) if t.quality_breakdown else None,
            'blend_breakdown': json.loads(t.blend_breakdown) if t.blend_breakdown else None,
            'excluded': (SOURCE_TEST, t.id) in excluded,
        })
    for r in Recording.query.filter_by(channel_id=channel_id).all():
        entries.append({
            'kind': 'recording', 'ts': r.completed_at or r.start_time, 'obj': r,
            'quality_breakdown': json.loads(r.health_quality_breakdown) if r.health_quality_breakdown else None,
            'blend_breakdown': json.loads(r.health_blend_breakdown) if r.health_blend_breakdown else None,
            'correction_breakdown': json.loads(r.capture_quality_breakdown) if r.capture_quality_breakdown else None,
            'excluded': (SOURCE_RECORDING, r.id) in excluded,
            'correction_excluded': (SOURCE_CAPTURE_CORRECTION, r.id) in excluded,
        })
    for e in ChannelEvent.query.filter_by(channel_id=channel_id).all():
        extra = json.loads(e.extra_data) if e.extra_data else {}
        kind = event_source_kind.get(e.event_type)
        entries.append({'kind': 'channel_event', 'ts': e.timestamp, 'obj': e,
                         'blend_breakdown': extra.get('blend_breakdown'),
                         'excluded': kind is not None and (kind, e.id) in excluded})
    entries.sort(key=lambda e: e['ts'], reverse=True)
    return entries


@channels_bp.app_template_filter('parse_json')
def parse_json_filter(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw) or {}
    except Exception:
        return {}


def _health_checks_for_channel(channel):
    """Every health check whose run would test this channel, newest job first.

    Resolves membership per job rather than building the whole monitored-channel
    set (`channel_tester.monitored_channel_ids()`), because this page asks about
    exactly one channel: a stored group is a membership lookup, and the system
    "TV Guide Channels" group's membership is computed (in_guide, minus
    test_enabled=False) and would otherwise load the entire guide. The
    disabled-member rule matches channel_groups.check_run_channels(): a run tests a
    member when both its membership's test_enabled and the channel-wide one are on."""
    membership_by_group = {m.group_id: m for m in channel.group_memberships}
    jobs = OnDemandTestJob.query.filter(OnDemandTestJob.group_id.isnot(None)).all()
    enrolled = []
    for job in jobs:
        group = job.group
        if group is None:
            continue
        if group.is_system:
            if channel.in_guide and channel.test_enabled:
                enrolled.append(job)
            continue
        membership = membership_by_group.get(group.id)
        if membership is None:
            continue
        if not membership.test_enabled:
            continue
        enrolled.append(job)
    enrolled.sort(key=lambda j: j.id, reverse=True)
    return enrolled


# Reorderable/hideable sections of the channel detail page, in default order
# (dev/changelog/341 - mockup 16's order). The header, status bar and the Recovery
# card are deliberately NOT in this set: they are alert-style chrome that must always
# render, the same rule the group page applies to its warning banners.
CHANNEL_DETAIL_SECTIONS = ('health', 'settings', 'whatson', 'timeline', 'url',
                           'recordings', 'tests')

# The "What's On" card is the TV Guide grid embedded as a single row, so it shares the
# guide's Layout machinery (app/routes/guide.py owns the defaults and the merge) but keeps
# its OWN pref keys. Two reasons, both deliberate: the card offers a strict SUBSET of the
# settings - it has no channel column, no sort and no channels to include or hide - and a
# control inside one channel's page must not silently reshape the whole TV Guide. Desktop
# and mobile split for the same reason the guide's do (DESIGN.md 12.3 as amended): a phone's
# field set must not reshape the desktop card.
WHATSON_LAYOUT_PREF_KEY = 'channel_detail_guide_layout_desktop'
WHATSON_LAYOUT_MOBILE_PREF_KEY = 'channel_detail_guide_layout_mobile'


def _quality_profile_stats(t):
    """Stream-quality stat cards for the Health card's grid (DESIGN-stream-quality-profile
    .md 4; dev/changelog/351). One flat list of {label, value, cls, tip}, appended to the
    existing Resolution/FPS/Frames/Bitrate/Drops/Audio cards - deliberately NOT a separate
    "Stream Profile" sub-section, since every one of these came out of the same test.

    Labels and tooltip wording come from app/fmt_utils.py, shared with the recording detail
    page and the group page's drawer.

    Returns [] when the test predates quality capture (all columns NULL), so the grid keeps
    its original six cards rather than gaining a row of dashes. Every card is emitted only
    for a value that is actually set, except the tri-state Scan/Frame rate pair, whose
    "Unknown" IS the measurement. Pure - no I/O, no queries.
    """
    if t is None or not t.video_codec:
        return []

    def stat(label, value, cls=None, tip=None):
        return {'label': label, 'value': value, 'cls': cls, 'tip': tip}

    stats = [stat('Codec', t.video_codec)]
    if t.bit_depth:
        stats.append(stat('Bit Depth', f'{t.bit_depth}-bit'))
    if t.chroma_subsampling:
        stats.append(stat('Chroma', fmt_utils.fmt_chroma(t.chroma_subsampling),
                          tip=fmt_utils.CHROMA_TIP))

    if t.interlaced is None:
        stats.append(stat('Scan', 'Unknown', tip=fmt_utils.SCAN_UNKNOWN_TIP))
    elif t.interlaced:
        stats.append(stat('Scan', 'Interlaced', cls='text-warning',
                          tip=fmt_utils.INTERLACED_TIP))
    else:
        stats.append(stat('Scan', 'Progressive', tip=fmt_utils.PROGRESSIVE_TIP))

    # The grid already carries an FPS card, so this one reports only the constant-vs-variable
    # verdict - repeating the number here would be two cards for one measurement.
    if t.is_vfr is None:
        stats.append(stat('Frame Rate', 'Unknown', tip=fmt_utils.VFR_UNKNOWN_TIP))
    elif t.is_vfr:
        stats.append(stat('Frame Rate', 'Variable', cls='text-warning', tip=fmt_utils.VFR_TIP))
    else:
        stats.append(stat('Frame Rate', 'Constant', tip=fmt_utils.CFR_TIP))

    if t.bits_per_pixel_frame:
        stats.append(stat('Efficiency', f'{t.bits_per_pixel_frame:.4f}',
                          tip=fmt_utils.EFFICIENCY_TIP))
    # Gaps are the exception to "always show the card": a clean clip has zero, and a zero
    # card would read as a finding where there is none.
    if t.timeline_gap_count:
        stats.append(stat(
            'Timeline Gaps',
            f'{t.timeline_gap_count} ({(t.timeline_gap_seconds or 0):.1f}s)',
            cls='text-danger', tip=fmt_utils.TIMELINE_GAP_TIP))

    # Multi-track detection (dev/changelog/564) - only when there's genuinely more than
    # one track of a type; a normal single-video/single-audio test shows nothing here.
    if (t.video_track_count or 0) > 1 or (t.audio_track_count or 0) > 1:
        parts = []
        if (t.video_track_count or 0) > 1:
            parts.append(f'{t.video_track_count} video')
        if (t.audio_track_count or 0) > 1:
            parts.append(f'{t.audio_track_count} audio')
        tip = fmt_utils.TRACKS_TIP
        try:
            extra = json.loads(t.extra_tracks) if t.extra_tracks else []
        except ValueError:
            extra = []
        lines = []
        for tr in extra:
            bits = [tr.get('codec')]
            if tr.get('type') == 'audio' and tr.get('channels'):
                bits.append(f"{tr['channels']}ch")
            if tr.get('resolution'):
                bits.append(tr['resolution'])
            if tr.get('language'):
                bits.append(tr['language'])
            label = (tr.get('type') or '?').capitalize()
            lines.append(f"{label}: " + ' '.join(b for b in bits if b))
        if lines:
            tip = tip + '\n' + '\n'.join(lines)
        stats.append(stat('Tracks', ', '.join(parts), tip=tip))
    return stats


@channels_bp.route('/api/channels/<int:channel_id>/logo')
def channel_logo(channel_id):
    """Serve a cached channel logo. resolve_logo_url() only ever points a page at this
    route when logo_cache_path is set, so a miss here means the file was removed from
    under us (or the row changed) between render and request - a 404 either way."""
    channel = db.session.get(Channel, channel_id)
    if channel is None or not channel.logo_cache_path:
        abort(404)
    resp = send_from_directory(get_logo_cache_dir(), channel.logo_cache_path)
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


@channels_bp.route('/channels/<int:channel_id>')
def channel_detail(channel_id):
    from ..database import UserPref

    channel = db.session.get(Channel, channel_id)
    if channel is None:
        abort(404)

    duplicate_channels = []
    if channel.is_duplicate_stream_url:
        duplicate_channels = (
            Channel.query
            .filter(Channel.stream_url == channel.stream_url, Channel.id != channel.id)
            .order_by(Channel.name)
            .all()
        )

    total_epg_count = EPGEntry.query.filter_by(channel_id=channel_id).count()

    per_page = request.args.get('per_page', '10')
    if per_page not in CHANNEL_HEALTH_PAGE_SIZES:
        per_page = '10'
    page = request.args.get('page', 1, type=int)

    test_query = ChannelTest.query.filter_by(channel_id=channel_id)\
        .order_by(ChannelTest.test_started_at.desc())

    if per_page == 'all':
        test_history = test_query.all()
        test_pagination = None
    else:
        test_pagination = test_query.paginate(page=page, per_page=int(per_page), error_out=False)
        test_history = test_pagination.items

    latest_test = ChannelTest.query.filter_by(channel_id=channel_id)\
        .order_by(ChannelTest.test_started_at.desc()).first()

    # A test row is created with status='FAILED' as a placeholder and only updated to its
    # real terminal status when it finishes (channel_tester.py::_finalize_test) - so a test
    # actively running for THIS channel right now is indistinguishable from a really-failed
    # one by status alone. The tester module's live state is the only place that knows
    # "in progress", so the row currently being tested (always the newest - it's the row
    # this run just inserted) is named explicitly rather than falling into the FAILED
    # branch (CLAUDE.md "one flag, one meaning; states are enumerated").
    tester_status = get_status()
    in_progress_test_id = (
        latest_test.id
        if latest_test and tester_status['is_running']
           and tester_status['current_channel_id'] == channel_id
        else None
    )

    # Both are pure formatting over rows already fetched above - no extra queries, so the
    # Test History page-size selector still costs one query at any size (test_scaling_pages).
    quality_stats = _quality_profile_stats(latest_test)
    test_profiles = {t.id: fmt_utils.profile_summary(
        t.video_codec, t.bit_depth, t.chroma_subsampling, t.interlaced, t.is_vfr,
        resolution=t.resolution) for t in test_history}

    final_health_score = None
    if channel.health_score is not None:
        final_health_score = int(round(
            max(0, min(100, channel.health_score + channel.manual_health_adjustment))
        ))

    recording_observations = (
        Recording.query
        .filter(Recording.channel_id == channel_id, Recording.health_quality_score.isnot(None))
        .order_by(Recording.completed_at.desc())
        .limit(10)
        .all()
    )

    timeline_per_page = request.args.get('timeline_per_page', '25')
    if timeline_per_page not in CHANNEL_HEALTH_PAGE_SIZES:
        timeline_per_page = '25'
    timeline_page = request.args.get('timeline_page', 1, type=int)

    all_timeline_entries = _build_channel_timeline(channel_id)
    if timeline_per_page == 'all':
        timeline_entries, timeline_pagination = all_timeline_entries, None
    else:
        per = int(timeline_per_page)
        timeline_pagination = _ListPagination(timeline_page, per, len(all_timeline_entries))
        start = (timeline_page - 1) * per
        timeline_entries = all_timeline_entries[start:start + per]

    recording_profiles = RecordingProfile.query.order_by(RecordingProfile.name).all()
    # "Create health check for this channel" opens the shared check-modal.js modal, which
    # needs the same profile readout the Browse tab's "Test selected" builds.
    health_check_profiles = HealthCheckProfile.query.order_by(HealthCheckProfile.name).all()

    # Every group this channel belongs to (badge links in the header). Single-channel
    # page: one relationship load, group joined-eager per membership.
    channel_kind_groups = [gm.group for gm in channel.group_memberships]
    # The subset whose own guide row carries this channel's listings. "In the guide
    # directly" and "in the guide via FS1" are two separate facts and the page states both
    # (dev/changelog/759); reading Channel.in_guide alone can only answer the first, and
    # answering only the first is what let this page tell a grouped channel it was not in
    # the TV Guide at all. Free - the group rows are already loaded above.
    guide_via_groups = [g for g in channel_kind_groups if g.in_guide]
    # The hide/un-hide sentence, built in one place so the header, the kebab and the sticky
    # bar cannot word the same state three ways. Free: `in_group` comes off the membership
    # rows already loaded on the line above, not a query of its own.
    # `epg_gap` costs no query of its own: total_epg_count is already fetched above, and the
    # hide purge deletes ALL of a channel's entries, so zero of them on a visible channel
    # that carries an EPG id is exactly the un-hidden-with-a-stale-guide case.
    hide = channel_hiding.hide_state(
        channel, in_group=bool(channel_kind_groups),
        epg_gap=(not channel.hidden and bool(channel.epg_channel_id)
                 and total_epg_count == 0))
    # Groups and health checks are two distinct things, so the header shows two chip
    # kinds rather than one "memberships" pile (dev/changelog/323).
    health_checks = _health_checks_for_channel(channel)
    # The guide-check enrollment switch in the Settings modal flips Channel.test_enabled
    # through the system job's own toggle route, so the page needs that job's id whenever
    # the channel is in the guide (enrolled or not - turning it back on needs it too).
    system_job = OnDemandTestJob.query.filter_by(is_system=True).first()
    guide_check_job_id = system_job.id if (system_job is not None and channel.in_guide) else None

    # Per-user section layout (order + hidden), server-side so it follows the user across
    # browsers - the generic user-prefs row the group page already uses (DESIGN.md 3.11).
    sec_pref = db.session.get(UserPref, 'channel_detail_sections')

    cfg = load_config()
    lifecycle_by_channel = _lifecycle_states_for_channels([channel], cfg)
    lifecycle_state, lifecycle_since = lifecycle_by_channel.get(channel.id, (None, None))
    repoint_candidate = _repoint_candidates_for_channels(
        [channel], cfg, lifecycle_by_channel).get(channel.id)

    # What's On is the guide grid with one row in it, so it takes the guide's own window,
    # tag list and Layout state (under this page's keys - see the constants above). Both
    # breakpoints' dicts are rendered because which one applies is a media query the server
    # cannot see. All four reads are per-request, never per row.
    from .guide import read_guide_layout
    guide_window_days = max(1, int(cfg.get('sync', {}).get('epg_days_ahead', 3) or 3))
    tags = Tag.query.order_by(Tag.name).all()
    whatson_layout = read_guide_layout(cfg, WHATSON_LAYOUT_PREF_KEY)
    whatson_layout_mobile = read_guide_layout(cfg, WHATSON_LAYOUT_MOBILE_PREF_KEY, mobile=True)

    from ..check_window import format_window_label
    window_label = format_window_label(cfg.get('channel_testing', {}))

    # "Search all EPG data" on the What's On card: every future airing on THIS channel,
    # not just what the guide row above happens to render. The `chan` dimension is
    # normally reached only from the search box's suggestion menu ("This exact
    # channel") - this is its second entry point.
    from ..channel_search import DimensionFilter
    chan_epg_search_url = airing_search_url(
        filters=(DimensionFilter('chan', (str(channel.id),), ()),))

    # How many step-backs are available and what the next one would leave the score at -
    # both promised in the confirm dialogs, so both come from the same replay that will run
    # (app/health_recompute.py). A fixed handful of queries, none of them per row.
    from ..health_recompute import rollback_preview
    health_rollback = rollback_preview(channel, cfg)

    return render_template('channels/detail.html',
        channel=channel,
        next_sync_at=(next_sync_map([channel.account])[channel.account_id]
                      if channel.account is not None else None),
        logo_url=resolve_logo_url(channel),
        window_label=window_label,
        channel_kind_groups=channel_kind_groups,
        guide_via_groups=guide_via_groups,
        hide=hide,
        health_checks=health_checks,
        lifecycle_state=lifecycle_state,
        not_normalizable=channel.id in _unnormalizable_channel_ids([channel], cfg),
        lifecycle_since=lifecycle_since,
        repoint_candidate=repoint_candidate,
        duplicate_channels=duplicate_channels,
        total_epg_count=total_epg_count,
        chan_epg_search_url=chan_epg_search_url,
        test_history=test_history,
        test_pagination=test_pagination,
        per_page=per_page,
        latest_test=latest_test,
        in_progress_test_id=in_progress_test_id,
        quality_stats=quality_stats,
        test_profiles=test_profiles,
        coded_tip=fmt_utils.CODED_TIP,
        final_health_score=final_health_score,
        health_rollback=health_rollback,
        pace_realtime_default=bool(cfg.get('ffmpeg', {}).get('pace_realtime', False)),
        recording_observations=recording_observations,
        timeline_entries=timeline_entries,
        timeline_pagination=timeline_pagination,
        timeline_per_page=timeline_per_page,
        recording_profiles=recording_profiles,
        check_profiles=health_check_profile_payload(
            cfg.get('channel_testing', {}), health_check_profiles),
        tester_status=tester_status,
        guide_check_job_id=guide_check_job_id,
        sections=list(CHANNEL_DETAIL_SECTIONS),
        section_pref=json.loads(sec_pref.value) if sec_pref and sec_pref.value else None,
        profiles=recording_profiles,  # _record_modal.html + GUIDE_CONFIG.profiles expect this name
        tags=tags,
        guide_window_days=guide_window_days,
        whatson_layout=whatson_layout,
        whatson_layout_pref_key=WHATSON_LAYOUT_PREF_KEY,
        whatson_layout_mobile=whatson_layout_mobile,
        whatson_layout_mobile_pref_key=WHATSON_LAYOUT_MOBILE_PREF_KEY,
    )


@channels_bp.route('/channels/<int:channel_id>/toggle', methods=['POST'])
@retry_on_locked()
def toggle_channel(channel_id):
    channel = db.session.get(Channel, channel_id)
    if channel is None:
        flash('Channel not found.', 'error')
        return redirect(url_for('channels.channel_browser'))

    next_url = request.form.get('next') or request.referrer or url_for('channels.channel_browser')

    # Group membership is not consulted: this toggles THIS channel's own row, and a member
    # is free to hold one alongside its group's (dev/changelog/751). The route used to
    # refuse outright for any member and flash "add the group on the Groups tab instead".
    if channel.in_guide:
        db.session.add(ChannelEvent(
            channel_id=channel.id, event_type=CHANNEL_REMOVED_FROM_GUIDE,
            detail=f'Removed from guide (was position {channel.guide_sort_order})',
        ))
        channel.in_guide = False
        flash(f'"{channel.name}" removed from guide.', 'success')
        # A guide row DEFERS a hide rather than refusing it, so losing the row is what
        # finally lets a deferred hide take effect. Same call in both directions - gaining
        # a row is what makes a hidden channel visible again.
        channel_hiding.recompute([channel.id])
        db.session.commit()
        return redirect(next_url)

    confirmed = request.form.get('confirm') == '1'
    if not confirmed:
        conflicts = _guide_conflicts(channel)
        if conflicts:
            from urllib.parse import urlencode
            names = ', '.join(c.name for c in conflicts)
            sep = '&' if '?' in next_url else '?'
            query = urlencode({'dup_block': channel.id, 'dup_names': names})
            return redirect(f'{next_url}{sep}{query}')

    # Place at end of guide. Channels and groups share one ordering space, so the
    # next slot must clear both (see channel_groups._next_guide_sort_order).
    channel.guide_sort_order = _next_guide_sort_order()
    channel.in_guide = True
    db.session.add(ChannelEvent(
        channel_id=channel.id, event_type=CHANNEL_ADDED_TO_GUIDE,
        detail=f'Added to guide at position {channel.guide_sort_order}',
    ))
    flash(f'"{channel.name}" added to guide.', 'success')

    channel_hiding.recompute([channel.id])
    db.session.commit()
    return redirect(next_url)


@channels_bp.route('/channels/<int:channel_id>/repoint', methods=['POST'])
def repoint_channel(channel_id):
    """Duplicate-repoint recovery action (DESIGN-sync-resilience.md §6): transfer guide/
    group/schedule/test-enrollment state from a missing-flagged channel to a surviving
    channel sharing the same stream_url. Server re-validates everything from DB ids - the
    POST body carries only survivor_channel_id, never DOM-derived state."""
    missing = db.session.get(Channel, channel_id)
    if missing is None:
        return jsonify({'error': 'Channel not found'}), 404

    body = request.get_json(silent=True) or {}
    survivor_id = body.get('survivor_channel_id')
    survivor = db.session.get(Channel, survivor_id) if survivor_id else None
    if survivor is None or survivor.id == missing.id:
        return jsonify({'error': 'Invalid survivor channel'}), 400
    if survivor.stream_url != missing.stream_url:
        return jsonify({'error': 'Channels no longer share a stream URL'}), 409

    cfg = load_config()
    lifecycle = _lifecycle_states_for_channels([missing, survivor], cfg)
    if lifecycle.get(missing.id, (None, None))[0] != 'missing':
        return jsonify({'error': 'Channel is no longer flagged missing'}), 409
    if lifecycle.get(survivor.id, (None, None))[0] == 'missing':
        return jsonify({'error': 'Survivor channel is also missing'}), 409

    @retry_on_locked()
    def _do_repoint_and_commit():
        m = db.session.get(Channel, missing.id)
        s = db.session.get(Channel, survivor.id)
        result_message = transfer_channel_state(m, s, cfg)
        db.session.commit()
        return result_message

    message = _do_repoint_and_commit()
    return jsonify({'success': True, 'message': message})


def _missing_days_bucket(days: int) -> str:
    if days < 14:
        return '7-14 days'
    if days < 30:
        return '14-30 days'
    if days < 90:
        return '30-90 days'
    return '90+ days'


_MISSING_DAYS_BUCKET_ORDER = ['7-14 days', '14-30 days', '30-90 days', '90+ days']

# Cap on how many blocked-channel rows the preview returns individually (each with a
# link, per the spec in dev/changelog/248) - a count beyond this is summarized rather than
# listed, so a pathological "everything is blocked" case can't bloat the response.
_BLOCKED_LIST_CAP = 200


def _serialize_blocked(channels):
    return [
        {'id': ch.id, 'name': ch.name,
         'url': url_for('channels.channel_detail', channel_id=ch.id)}
        for ch in channels[:_BLOCKED_LIST_CAP]
    ]


class _BadRequestedIds(ValueError):
    """A client-supplied channel_ids list this server will not act on."""


def _parse_requested_ids(raw):
    """None (no explicit-id scope), or the requested ids as a de-duplicated list.

    Raises _BadRequestedIds for anything that is not a list of integers, or a list longer
    than _MAX_REQUESTED_DELETE_IDS. An empty list is a legal request that selects nothing -
    distinct from None, which means the caller is using the account or group scope instead.
    Accepts a comma-separated string as well, so the preview's query string and the delete's
    JSON body spell the same scope the same way.
    """
    if raw is None or raw == '':
        return None
    if isinstance(raw, str):
        raw = [part for part in raw.split(',') if part.strip()]
    if not isinstance(raw, list):
        raise _BadRequestedIds('channel_ids must be a list of channel ids')
    out = []
    seen = set()
    for item in raw:
        try:
            # bool is an int subclass and True would silently read as channel 1.
            if isinstance(item, bool):
                raise TypeError
            cid = int(item)
        except (TypeError, ValueError):
            raise _BadRequestedIds('channel_ids must be a list of channel ids')
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    if len(out) > _MAX_REQUESTED_DELETE_IDS:
        raise _BadRequestedIds(
            f'Too many channels in one request (limit {_MAX_REQUESTED_DELETE_IDS})')
    return out


def _refusal_reason(requested, eligible, blocked_inuse, blocked_active):
    """None when the delete may proceed, else prose naming why an explicitly-requested
    delete can act on nothing at all.

    Only the explicit-id scopes get one. An account or group sweep that finds nothing
    eligible is a legitimate no-op - "nothing to clean up" - so it keeps returning success
    with a count of zero, exactly as it always has.

    Every state is named: the alternative is answering a delete that did nothing with
    success and a zero count, which is the silence product principle 1 exists against.
    A request mixing several reasons is reported by the one that blocks the most, since
    with nothing eligible there is no partial success to describe.
    """
    if requested is None or eligible:
        return None
    if not requested:
        return 'No channels were selected.'
    n_active, n_inuse = len(blocked_active), len(blocked_inuse)
    n_gone = len(requested) - n_active - n_inuse
    one = len(requested) == 1
    subject = 'That channel' if one else 'None of those channels'
    if n_active >= n_inuse and n_active >= n_gone:
        return (f'{subject} can be deleted - '
                + ('it has' if one else 'they have')
                + ' a recording scheduled or in progress. Unschedule or abort it first.')
    if n_inuse >= n_gone:
        return (f'{subject} can be deleted - '
                + ('it is' if one else 'they are')
                + ' in the TV Guide or in a channel group. Remove '
                + ('it' if one else 'them') + ' from there first.')
    return (f'{subject} can be deleted - '
            + ('it is' if one else 'they are')
            + " still listed in the provider's feed. Only channels the provider has "
              'stopped listing can be deleted.')


def _serialize_not_missing(channel_ids):
    """Name the not-missing ids for display, capped like the blocked lists.

    One keyed query, never a get() per id. An id with no row is rendered from the id
    alone rather than dropped: "a channel that no longer exists" and "a channel still in
    the feed" are different answers and the reader is owed whichever one applies.
    """
    if not channel_ids:
        return []
    shown = channel_ids[:_BLOCKED_LIST_CAP]
    names = {
        cid: name for cid, name in
        db.session.query(Channel.id, Channel.name).filter(Channel.id.in_(shown)).all()
    }
    return [
        {'id': cid,
         'name': names.get(cid, f'Channel #{cid}'),
         'url': url_for('channels.channel_detail', channel_id=cid),
         'exists': cid in names}
        for cid in shown
    ]


def _not_missing_ids(requested, eligible, blocked_inuse, blocked_active):
    """The requested ids that the partition placed nowhere: still listed in the provider's
    feed, or already gone from the database.

    Reported rather than passed over, because on an explicitly-requested delete "nothing
    happened to this one" with no reason given is exactly the silence product principle 1
    exists against. Meaningless for the account and group scopes, whose candidate set is
    derived rather than requested - both send requested=None.
    """
    if requested is None:
        return []
    placed = {ch.id for ch in eligible}
    placed.update(ch.id for ch in blocked_inuse)
    placed.update(ch.id for ch in blocked_active)
    return [cid for cid in requested if cid not in placed]


@channels_bp.route('/api/channels/missing-delete-preview')
def missing_delete_preview():
    """Read-only counts for the bulk-delete-missing-channels modal (dev/changelog/248,
    "Bulk-delete channels missing from the provider feed"). Scoped by account_id the
    same way the Browse page's own filter is - no account_id means all accounts. Also
    scoped by group_id, for the same modal reused on a group/health check's detail page
    (dev/changelog/653). And by an explicit comma-separated channel_ids, for the same
    modal reused as the confirm step of the channel detail page's single delete and the
    Browse tab's selection delete (dev/changelog/772) - and, combined with group_id, that
    same group/health-check page's own selection-delete, narrowed to just the channels
    picked there rather than the whole group."""
    account_id = request.args.get('account_id', type=int)
    group_id = request.args.get('group_id', type=int)
    try:
        requested_ids = _parse_requested_ids(request.args.get('channel_ids'))
    except _BadRequestedIds as err:
        return jsonify({'error': str(err)}), 400
    cfg = load_config()
    eligible, blocked_inuse, blocked_active = _missing_delete_candidates(
        account_id, cfg, group_id=group_id, channel_ids=requested_ids)
    not_missing = _not_missing_ids(requested_ids, eligible, blocked_inuse, blocked_active)

    now = datetime.utcnow()
    bucket_counts = {label: 0 for label in _MISSING_DAYS_BUCKET_ORDER}
    for ch in eligible:
        bucket_counts[_missing_days_bucket((now - ch.last_seen_at).days)] += 1

    account = db.session.get(Account, account_id) if account_id and not group_id else None
    return jsonify({
        'success': True,
        'account_name': account.name if account else None,
        'eligible_count': len(eligible),
        # Hidden channels stay eligible for this sweep (decided: hidden means out of the
        # way, not protected), so the preview says how many of the batch the user cannot
        # currently see in the search, rather than a delete count the search header cannot
        # account for.
        'eligible_hidden_count': sum(1 for ch in eligible if ch.hidden),
        'buckets': [{'label': label, 'count': bucket_counts[label]} for label in _MISSING_DAYS_BUCKET_ORDER],
        'blocked_inuse_count': len(blocked_inuse),
        'blocked_inuse': _serialize_blocked(blocked_inuse),
        'blocked_active_count': len(blocked_active),
        'blocked_active': _serialize_blocked(blocked_active),
        'not_missing_count': len(not_missing),
        'not_missing': _serialize_not_missing(not_missing),
    })


@channels_bp.route('/channels/missing-delete', methods=['POST'])
def missing_delete():
    """Bulk-delete channels missing from the provider feed (dev/changelog/248). A
    deliberate exception to "channels are never hard-deleted"
    (DESIGN-sync-resilience.md §7) for exactly this case: a channel with no candidate to
    re-point to (sync resilience D, changelog/246) that a normal sync will never clean up
    on its own.

    Three scopes, same teardown: a whole account, a group/health check's own channels
    (dev/changelog/653), or an explicitly requested channel_ids set - the detail page's
    single delete, the Browse tab's selection delete (dev/changelog/772), and that same
    group/health-check page's own selection-delete, which sends both a group_id and a
    channel_ids together to narrow the group scope down to just the selection.

    Whichever scope is used, the server recomputes the eligible set itself and never trusts
    a client's claim about it (same shape as repoint_channel's re-validation above). The
    id list narrows WHICH channels are considered; it never asserts that one is missing
    from its feed or that it is unblocked, both of which are re-derived here."""
    from ..recorder import delete_files

    body = request.get_json(silent=True) or {}
    raw_account_id = body.get('account_id')
    account_id = int(raw_account_id) if raw_account_id not in (None, '') else None
    raw_group_id = body.get('group_id')
    group_id = int(raw_group_id) if raw_group_id not in (None, '') else None
    try:
        requested_ids = _parse_requested_ids(body.get('channel_ids'))
    except _BadRequestedIds as err:
        return jsonify({'error': str(err)}), 400
    # Explicit whenever specific ids were named, group_id or not: a selection-delete
    # inside a group is exactly as "the user asked about these ids" as one with no group
    # at all, and deserves the same refusal/not-missing reporting rather than the silent
    # zero-count a bare group/account sweep gets.
    explicit = requested_ids is not None
    cfg = load_config()

    @retry_on_locked()
    def _do_delete_and_commit():
        eligible, blocked_inuse, blocked_active = _missing_delete_candidates(
            account_id, cfg, group_id=group_id, channel_ids=requested_ids)
        # What the partition decided is carried back out of the closure rather than
        # discarded: an explicitly-requested delete that acted on nothing has to say why,
        # and one that acted on only some of what was asked has to say how many it left.
        # Read here, inside the same unit that decided it, so both describe the state the
        # delete actually saw.
        skipped = {
            'blocked_inuse_count': len(blocked_inuse),
            'blocked_active_count': len(blocked_active),
            'not_missing_count': len(_not_missing_ids(
                requested_ids if explicit else None,
                eligible, blocked_inuse, blocked_active)),
        }
        refusal = _refusal_reason(
            requested_ids if explicit else None, eligible, blocked_inuse, blocked_active)
        ids = [ch.id for ch in eligible]
        if not ids:
            return 0, 0, [], set(), refusal, skipped
        touched_account_ids = {ch.account_id for ch in eligible}

        # Screenshot files aren't part of the DB and won't go away with the row
        # (CLAUDE.md "teardown releases everything the create path acquired"), so their
        # paths are read while the ChannelTest rows that carry them still exist. Only the
        # paths are collected here - the unlink itself is a non-idempotent side effect and
        # runs after this closure has durably committed, mirroring delete_recording.
        screenshot_paths = []
        for chunk in _chunked(ids, _DELETE_CHUNK_SIZE):
            screenshot_paths.extend(
                p for (p,) in db.session.query(ChannelTest.screenshot_path)
                .filter(ChannelTest.channel_id.in_(chunk))
                .filter(ChannelTest.screenshot_path.isnot(None)).all()
            )

        # Which groups are about to lose a member, read while the membership rows still
        # exist. Any of them left in the TV Guide with nothing switched on for recording
        # is DESIGN-channel-groups-model.md 15's breach path 3 with nobody present to
        # confirm it, so it gets an alert after the commit rather than an action here.
        touched_group_ids = set()
        for chunk in _chunked(ids, _DELETE_CHUNK_SIZE):
            touched_group_ids.update(
                gid for (gid,) in db.session.query(ChannelGroupMember.group_id)
                .filter(ChannelGroupMember.channel_id.in_(chunk)).distinct().all())

        unlinked_recordings = 0
        for chunk in _chunked(ids, _DELETE_CHUNK_SIZE):
            unlinked_recordings += (
                db.session.query(Recording)
                .filter(Recording.channel_id.in_(chunk))
                .filter(Recording.status.in_(TERMINAL_RECORDING_STATUSES))
                .update({'channel_id': None}, synchronize_session=False)
            )
            db.session.query(ChannelGroupMember).filter(
                ChannelGroupMember.channel_id.in_(chunk)).delete(synchronize_session=False)
            db.session.query(ChannelTest).filter(
                ChannelTest.channel_id.in_(chunk)).delete(synchronize_session=False)
            db.session.query(ChannelEvent).filter(
                ChannelEvent.channel_id.in_(chunk)).delete(synchronize_session=False)
            db.session.query(EPGEntry).filter(
                EPGEntry.channel_id.in_(chunk)).delete(synchronize_session=False)
            db.session.query(Channel).filter(
                Channel.id.in_(chunk)).delete(synchronize_session=False)

        # account.channel_count/epg_entry_count and Channel.is_duplicate_stream_url are all
        # stored counters that only self-heal on the next full sync
        # (_mark_success_and_commit / _recompute_duplicate_stream_urls in app/accounts.py) -
        # a bulk delete is exactly the kind of out-of-band change that leaves them stale
        # (dashboard/accounts-page counts wrong; surviving channels still flagged DUP)
        # without an explicit recompute here, and repeated real syncs aren't always an
        # option (provider rate limiting).
        for acct_id in touched_account_ids:
            acct = db.session.get(Account, acct_id)
            if acct is None:
                continue
            acct.channel_count = Channel.query.filter_by(account_id=acct_id).count()
            acct.epg_entry_count = EPGEntry.query.join(Channel).filter(
                Channel.account_id == acct_id).count()
        # This delete removes rows outright rather than going through channel_hiding's one
        # recompute, so it is the one other caller of the counter that recompute() would
        # otherwise keep in sync on its own - a deleted hidden channel must leave the count.
        channel_hiding.refresh_hidden_channel_counts(touched_account_ids)
        _recompute_duplicate_stream_urls()

        db.session.commit()
        return len(ids), unlinked_recordings, screenshot_paths, touched_group_ids, None, skipped

    (deleted_count, unlinked_recordings, screenshot_paths,
     touched, refusal, skipped) = _do_delete_and_commit()
    # Only the explicit-id scopes produce a refusal. An account or group sweep with nothing
    # eligible is a legitimate no-op ("nothing to clean up"), not a request that failed.
    if refusal is not None:
        log.info('Channel delete refused (%d requested): %s', len(requested_ids), refusal)
        return jsonify({'error': refusal}), 409
    delete_files(screenshot_paths)
    broken = report_orphaned_guide_groups(
        touched, cause='The channels it could record from were deleted as missing from '
                       'the provider.')
    log.info('Missing-channel bulk delete: %d channel(s) deleted (account_id=%s, group_id=%s, '
             'requested=%s), %d terminal recording(s) unlinked', deleted_count, account_id,
             group_id, len(requested_ids) if requested_ids is not None else None,
             unlinked_recordings)
    return jsonify({
        'success': True,
        'deleted_count': deleted_count,
        'recordings_preserved': unlinked_recordings,
        # Named in the response as well as alerted: the user IS present for this one, they
        # are just not looking at those groups. Telling them here is cheaper than making
        # them find the alert.
        'broken_guide_groups': len(broken),
        # Same reasoning one step down: a selection delete that took 7 of the 10 asked for
        # reports the 3 it left and why, rather than letting the count quietly disagree
        # with what was selected. All zero for the account and group sweeps.
        **skipped,
    })


@channels_bp.route('/api/channels/<int:channel_id>/test-now', methods=['POST'])
def test_channel_now(channel_id):
    """One-off "does this play right now?" test for a single channel (channel detail page).

    Not a health check and not a job: it writes a plain ChannelTest with job_id NULL, so
    it feeds the lifetime score and the channel's Test History without leaving a job or a
    group behind. Both gates are checked here so the user is told why nothing
    happened rather than watching a run that never starts:
      - the tester runs one thing at a time globally;
      - the account's connection limit is shared with recordings.
    The limit peek can still lose a race with a recording starting in between, which
    run_channel_test()'s own try_acquire() catches - that is the authority, this is the
    explanation."""
    from ..channel_tester import get_status, run_single_channel_test
    from .. import connection_limits as connlim

    channel = db.session.get(Channel, channel_id)
    if channel is None:
        return jsonify({'error': 'Channel not found'}), 404

    if get_status()['is_running']:
        return jsonify({'error': 'A health check is already running - please wait for it to finish'}), 409
    if connlim.at_limit(channel.account_id):
        return jsonify({'error': f'{channel.account.name} is at its connection limit right now '
                                 f'(a recording or another test is using it)'}), 409

    app = current_app._get_current_object()
    threading.Thread(target=run_single_channel_test, args=(app, channel_id),
                     daemon=True, name=f'test-now-{channel_id}').start()
    return jsonify({'success': True,
                    'message': f'Testing "{channel.name}" now - this page will refresh when it finishes.'})


@channels_bp.route('/channels/<int:channel_id>/notes', methods=['POST'])
@retry_on_locked()
def update_channel_notes(channel_id):
    channel = db.session.get(Channel, channel_id)
    if channel is None:
        return jsonify({'error': 'Channel not found'}), 404
    channel.notes = (request.get_json(silent=True) or {}).get('notes', '').strip() or None
    db.session.commit()
    return jsonify({'success': True, 'notes': channel.notes or ''})


@channels_bp.route('/channels/<int:channel_id>/default-profile', methods=['POST'])
@retry_on_locked()
def update_channel_default_profile(channel_id):
    channel = db.session.get(Channel, channel_id)
    if channel is None:
        return jsonify({'error': 'Channel not found'}), 404
    raw = (request.get_json(silent=True) or {}).get('profile_id')
    profile_id = int(raw) if raw not in (None, '') else None
    channel.default_profile_id = profile_id
    db.session.commit()
    return jsonify({'success': True, 'default_profile_id': channel.default_profile_id})


@channels_bp.route('/channels/<int:channel_id>/pace-realtime', methods=['POST'])
@retry_on_locked()
def update_channel_pace_realtime(channel_id):
    """Body {pace_realtime: true | false | null}; null follows ffmpeg.pace_realtime.

    The one writer of Channel.pace_realtime - a user's answer, which the watchdog's
    automatic pacing never writes (dev/changelog/997)."""
    channel = db.session.get(Channel, channel_id)
    if channel is None:
        return jsonify({'error': 'Channel not found'}), 404
    data = request.get_json(silent=True) or {}
    value = data.get('pace_realtime', 'missing')
    if not (value is None or isinstance(value, bool)):
        return jsonify({'error': 'pace_realtime must be true, false or null'}), 400
    channel.pace_realtime = value
    db.session.commit()
    return jsonify({'success': True, 'pace_realtime': channel.pace_realtime})


@channels_bp.route('/channels/<int:channel_id>/health-adjustment', methods=['POST'])
@retry_on_locked()
def update_health_adjustment(channel_id):
    """JSON since the channel-detail revamp - the offset is edited in that page's
    Settings modal, which posts alongside the notes and default-profile endpoints
    rather than through a form-post-and-redirect of its own."""
    channel = db.session.get(Channel, channel_id)
    if channel is None:
        return jsonify({'error': 'Channel not found'}), 404

    data = request.get_json(silent=True) or {}
    try:
        adjustment = int(data.get('adjustment') or 0)
    except (TypeError, ValueError):
        adjustment = 0
    new_adjustment = max(-100, min(100, adjustment))
    new_note = (data.get('note') or '').strip() or None
    if new_adjustment != channel.manual_health_adjustment or new_note != channel.manual_health_note:
        db.session.add(ChannelEvent(
            channel_id=channel.id, event_type=CHANNEL_HEALTH_OVERRIDE_CHANGED,
            detail=f'Manual adjustment changed {channel.manual_health_adjustment:+d} → {new_adjustment:+d}',
            extra_data=json.dumps({
                'old_adjustment': channel.manual_health_adjustment, 'new_adjustment': new_adjustment,
                'old_note': channel.manual_health_note, 'new_note': new_note,
            }),
        ))
    channel.manual_health_adjustment = new_adjustment
    channel.manual_health_note = new_note
    db.session.commit()
    return jsonify({'success': True, 'adjustment': new_adjustment, 'note': new_note or ''})


# ---------------------------------------------------------------------------
# Health score rollback - reset, and step back one observation
# ---------------------------------------------------------------------------

_ROLLBACK_ACTIONS = ('reset', 'step_back')


@channels_bp.route('/channels/<int:channel_id>/health/rollback', methods=['POST'])
def rollback_health_score(channel_id):
    """Unwind observations from a channel's health score, by hand.

    `action` is 'reset' (stop counting every observation - the channel goes back to having
    no score at all, as if it had never been tested) or 'step_back' (stop counting the
    single newest one, repeatable). Both are one replay of what remains, not a subtraction:
    app/health_recompute.py explains why, and holds the engine.

    Returns the fresh preview alongside the result, so the page can restate how many
    step-backs are left without a second round trip.
    """
    from ..health_recompute import apply_rollback, rollback_preview

    channel = db.session.get(Channel, channel_id)
    if channel is None:
        return jsonify({'error': 'Channel not found'}), 404

    action = (request.get_json(silent=True) or {}).get('action')
    if action not in _ROLLBACK_ACTIONS:
        return jsonify({'error': "action must be 'reset' or 'step_back'"}), 400

    cfg = load_config()

    @retry_on_locked()
    def _rollback_and_commit():
        ch = db.session.get(Channel, channel_id)
        result = apply_rollback(ch, action, cfg)
        if result is None:
            return None
        db.session.commit()
        return result

    result = _rollback_and_commit()
    if result is None:
        return jsonify({'error': 'This channel has no observations left to unwind - its '
                                 'health score is already reset'}), 409

    return jsonify({'success': True, 'result': result,
                    'preview': rollback_preview(channel, cfg)})


# ---------------------------------------------------------------------------
# Hiding
# ---------------------------------------------------------------------------

#: The JSON body's `override` value, as a tri-state. Spelled as the column rather than as a
#: verb ("hide"/"show") because the third value is the one that matters: undoing a hand-hide
#: means going back to following the rules, which is NOT the same as asserting "never hide
#: this channel" - that is False, and it survives every rule written afterwards.
_OVERRIDE_VALUES = (True, False, None)


def _parse_override(data):
    """The requested override, or ValueError. `is`, not `in`: True and 1 hash equal, so a
    membership test would accept a JSON `1` for a column three functions read as a
    tri-state. `_BadRequestedIds` is a ValueError too, so one `except` covers both parsers."""
    if 'override' not in data:
        raise ValueError('override is required: true, false or null')
    raw = data['override']
    if not any(raw is v for v in _OVERRIDE_VALUES):
        raise ValueError('override must be true, false or null')
    return raw


@channels_bp.route('/channels/<int:channel_id>/hide', methods=['POST'])
def hide_channel(channel_id):
    """Set one channel's hide override by hand, and rewrite its effective answer.

    A channel in the TV Guide or in a channel group is NOT refused - the override is stored
    and the channel keeps showing up until that membership goes away, at which point it
    hides itself. The response says which happened, and the page renders the sentence
    `channel_hiding.hide_state()` builds rather than wording it a second time.
    """
    channel = db.session.get(Channel, channel_id)
    if channel is None:
        return jsonify({'error': 'Channel not found'}), 404
    try:
        override = _parse_override(request.get_json(silent=True) or {})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    # One retried unit over the whole read-modify-write - the override, the event and the
    # recompute that reads them - with the re-fetch inside the closure, since a rollback
    # expires the pending attribute change a retry would otherwise silently drop.
    @retry_on_locked()
    def _do_hide_and_commit():
        ch = db.session.get(Channel, channel_id)
        moved = channel_hiding.set_hidden_override(ch, override)
        channel_hiding.recompute([channel_id])
        db.session.commit()
        return moved

    moved = _do_hide_and_commit()
    fresh = db.session.get(Channel, channel_id)
    in_group = bool(channel_hiding.channels_in_any_group([channel_id]))
    # Read after the commit, so it sees the purge the recompute just ran rather than the
    # entries that existed a moment ago.
    epg_gap = (not fresh.hidden and bool(fresh.epg_channel_id)
               and EPGEntry.query.filter_by(channel_id=channel_id).count() == 0)
    return jsonify({'success': True, 'moved': moved,
                    'hide': channel_hiding.hide_state(fresh, in_group=in_group,
                                                      epg_gap=epg_gap)})


@channels_bp.route('/api/channels/hide', methods=['POST'])
def hide_channels_bulk():
    """Hide (or un-hide) a selection from the channel search.

    Deferral is what makes this a batch rather than an all-or-nothing: 100 selected channels
    of which 1 is in the guide hides 99 and reports the 1 by name, instead of failing the
    whole request over one row. That is deliberate, and it is the same shape a rule match
    takes, so the two doors cannot disagree about what protection means.
    """
    data = request.get_json(silent=True) or {}
    try:
        ids = _parse_requested_ids(data.get('channel_ids'))
        override = _parse_override(data)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    if not ids:
        return jsonify({'error': 'Select some channels first.'}), 400

    @retry_on_locked()
    def _do_bulk_hide_and_commit():
        # Re-fetched inside the closure, per the retry rule: a rolled-back session expires
        # every pending change, so a retry that reused rows fetched outside would commit an
        # empty transaction and report success.
        rows = Channel.query.filter(Channel.id.in_(ids)).all()
        for ch in rows:
            channel_hiding.set_hidden_override(ch, override)
        channel_hiding.recompute([ch.id for ch in rows])
        db.session.commit()
        return [ch.id for ch in rows]

    found = _do_bulk_hide_and_commit()
    if not found:
        return jsonify({'error': 'None of those channels exist any more.'}), 404

    # After the commit, so these read the recomputed answer rather than the pre-flush one.
    fresh = Channel.query.filter(Channel.id.in_(found)).order_by(Channel.name).all()
    deferred = [{'id': ch.id, 'name': ch.name} for ch in fresh if ch.hidden_deferred]
    hidden = sum(1 for ch in fresh if ch.hidden)
    # How many of the now-visible ones have no guide data left - hiding deleted it and
    # un-hiding does not fetch it back. One grouped query for the whole batch rather than a
    # count per row, since a selection here is routinely thousands of channels.
    visible_with_epg_id = [ch.id for ch in fresh
                           if not ch.hidden and ch.epg_channel_id]
    epg_gap = 0
    if visible_with_epg_id:
        with_entries = {row[0] for row in db.session.query(EPGEntry.channel_id).filter(
            EPGEntry.channel_id.in_(visible_with_epg_id)).distinct()}
        epg_gap = len(visible_with_epg_id) - len(with_entries)
    return jsonify({'success': True, 'requested': len(ids), 'matched': len(found),
                    'hidden': hidden, 'deferred': deferred, 'epg_gap': epg_gap,
                    'missing': len(ids) - len(found)})
