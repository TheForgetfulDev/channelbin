import json
import threading
from datetime import datetime, timedelta

from flask import (Blueprint, render_template, request, redirect, url_for, flash, current_app,
                   abort, jsonify)

from .. import db
from ..accounts import (NORM_DISABLED, NORM_MODES, coerce_normalization_mode,
                        norm_mode_example, norm_mode_label, normalize_url_with_mode,
                        resolve_normalization_mode, url_is_normalizable,
                        recompute_duplicate_stream_urls_and_commit, finalize_sync_state,
                        next_sync_map, report_source_removed, resolve_source_alerts,
                        start_source_refresh, sync_signature, update_source_stale_alert)
from .. import account_stats_view
from ..account_stats import WINDOWS, today_local, windowed_stats
from ..channel_groups import report_orphaned_guide_groups
from ..channel_search import GROUP_ANY, OTHER_NEW, OTHER_REMOVED
from ..config import load_config, config_default
from ..database import (Account, AccountStatDay, Channel, AccountSyncLog, ChannelGroupMember,
                        EpgSource, EpgSourceSubscription, EPG_SOURCE_PROVIDER,
                        EPG_SOURCE_URL)
from ..db_utils import retry_on_locked
from ..epg_sources import (MATCH_DISAGREE, MATCH_NEW, REFRESH_HOURS_CHOICES,
                           accept_name_matches, account_sources_view,
                           add_url_source, borrowable_sources, clean_url_source_fields,
                           delete_sources_for_account, delete_url_source,
                           ensure_provider_source, foreign_guided_channels, foreign_readers, has_directory,
                           forget_reader, move_source, name_match_review, propose_again,
                           reject_name_matches, rejected_name_matches, reresolve_channels,
                           stop_using_source, subscribe_to_source, update_url_source,
                           use_source_again)
from ..logo_cache import delete_cached_logos
from ..tz_utils import format_local, get_display_tz, is_24h
from ..ui_constants import PRESET_COLORS
from ..url_utils import mask_url_path
from .channels import _chunked, _DELETE_CHUNK_SIZE

accounts_bp = Blueprint('accounts', __name__)

# The five sections of the account page, in their default order (DESIGN.md §17.3). The
# user's own order/hidden picks ride on the same server-side user-prefs store the group
# detail page uses, so a saved layout follows them across browsers - and a layout saved
# before a section existed shows it at its default place (util.js initSectionLayout).
ACCOUNT_SECTIONS = ['details', 'content', 'sources', 'usage', 'history', 'activity']

# How many sync runs the Sync history section shows before its "All N syncs" control.
# Expanding loads the rest in place - there is no separate history page any more
# (dev/changelog/455; DESIGN.md §17.1 retired account_logs.html).
HISTORY_SHOWN = 10

# The per-account sync-interval choices. Blank means "follow sync.sync_interval_hours".
_SYNC_HOURS_CHOICES = [1, 2, 4, 6, 12, 24]

# How long a delete waits for a cancelled sync thread to exit before refusing. Bounded
# because this runs inside the request: a sync only checks for the stop signal at phase
# boundaries, so waiting longer would hold the request open without improving the odds.
_SYNC_STOP_TIMEOUT_SECONDS = 5.0

# One row in the generic user-prefs store holds this page's section order and hidden set,
# so the layout follows the user across browsers (DESIGN.md §3.11).
_SECTIONS_PREF_KEY = 'account_detail_sections'

# ('' = defer to the global default) + the four modes. The example strings are rendered
# live under the dropdown so the mode names are unambiguous - see changelog/258 Spec §1.
_NORM_OPTIONS = [('', 'Use global default', '')] + [
    (value, label, example) for value, label, example in NORM_MODES
]


def _norm_example(mode_value: str) -> str:
    """Example URL for the currently-selected mode, so the hint under the dropdown is
    already correct server-side and JS only has to keep it in step. '' (defer to the
    global default) has no example of its own and correctly falls through to ''."""
    return norm_mode_example(mode_value)


def _recent_logs_by_account(account_ids, limit=5):
    """The last `limit` sync runs for each account, in ONE query.

    A query per account here was a real N+1 - the exact defect the Tags page had
    (`dev/docs/BUGS.md` 2026-08-04 @ 05:58:52 AM ET) and the reason `/accounts` needs a case
    in tests/test_scaling_pages.py. Ranking in SQL rather than fetching every log and
    slicing in Python matters because a long-lived account accumulates thousands of runs
    while the page only ever shows five."""
    if not account_ids:
        return {}
    ranked = (
        db.session.query(
            AccountSyncLog.id.label('id'),
            db.func.row_number().over(
                partition_by=AccountSyncLog.account_id,
                order_by=AccountSyncLog.started_at.desc(),
            ).label('rn'),
        )
        .filter(AccountSyncLog.account_id.in_(account_ids))
        .subquery()
    )
    rows = (
        AccountSyncLog.query
        .join(ranked, ranked.c.id == AccountSyncLog.id)
        .filter(ranked.c.rn <= limit)
        .order_by(AccountSyncLog.account_id, AccountSyncLog.started_at.desc())
        .all()
    )
    out = {}
    for log in rows:
        out.setdefault(log.account_id, []).append(log)
    return out


def _sync_log_totals(account_ids):
    """{account_id: how many sync runs it has ever had}, in one query."""
    if not account_ids:
        return {}
    return dict(
        db.session.query(AccountSyncLog.account_id, db.func.count(AccountSyncLog.id))
        .filter(AccountSyncLog.account_id.in_(account_ids))
        .group_by(AccountSyncLog.account_id)
        .all()
    )


@accounts_bp.route('/accounts')
def accounts_list():
    """The Accounts list (DESIGN.md §17.1) and, below it, the account stats (§17.7). A row
    says which account this is, whether it is healthy, how big it is, and what it is right
    now; the stats section compares what the accounts did over a window.

    Everything the old fat card needed and this one does not (recording totals, the preset
    color swatches, the debug flag) is gone rather than passed and ignored: an unused
    template variable is a query the page pays for on every load."""
    # Read BEFORE the rows. A sync that commits in between then shows up as a mismatch at the
    # next poll and costs one extra refresh; read after, the page would claim a signature
    # its rows do not reflect and never refresh for that change at all.
    try:
        window = account_stats_view.resolve_window(request.args.get('w'))
    except ValueError:
        # Validated here, not only by the chips: a hand-typed or stale link is a 400 that
        # names the choices, never a silent fall back to some other window.
        abort(400, description=f"w must be one of {', '.join(WINDOWS)}")
    sync_sig = sync_signature()
    # First, before anything else is loaded: it may commit a ledger catch-up, which expires
    # every row already in the session. It also loads the accounts, so the rows' second
    # line and the stats section below the list come from one call (dev/changelog/1029).
    stats = account_stats_view.section_context(window, load_config(),
                                               current_app._get_current_object())
    accounts = stats['accounts']
    account_ids = [a.id for a in accounts]
    logs_by_account = _recent_logs_by_account(account_ids, limit=5)
    guide_counts_by_account = {aid: cur['guide_channels'] for aid, cur in stats['current'].items()}
    # The header's channel total is the sum of the column beneath it, deliberately - a
    # separately-queried total that disagreed with the visible rows would be worse than none.
    total_channels = sum(a.channel_count or 0 for a in accounts)
    total_hidden_channels = sum(a.hidden_channel_count or 0 for a in accounts)
    return render_template(
        'accounts.html',
        accounts=accounts,
        next_sync=next_sync_map(accounts),
        logs_by_account=logs_by_account,
        guide_counts=guide_counts_by_account,
        total_channels=total_channels,
        total_hidden_channels=total_hidden_channels,
        sync_sig=sync_sig,
        stats=stats,
    )


def _sync_log_json(log):
    """One sync run, in the one shape every consumer reads.

    The page's first paint and the expand-in-place fetch both go through this, so the
    history region has exactly one renderer fed by exactly one shape - a second spelling
    is how a first paint and a refresh end up disagreeing."""
    return {
        'id': log.id,
        'started_at': log.started_at.isoformat() if log.started_at else None,
        'completed_at': log.completed_at.isoformat() if log.completed_at else None,
        'status': log.status,
        'channels': log.channels_synced or 0,
        'epg': log.epg_entries_synced or 0,
        'duration_seconds': ((log.completed_at - log.started_at).total_seconds()
                             if log.completed_at and log.started_at else None),
        'error': log.error_message or '',
    }


def _section_pref():
    """The stored {order, hidden} for the account page's sections, or None.

    It is user-written JSON, so anything that is not the expected shape falls back to the
    defaults rather than being allowed to 500 a page it only arranges."""
    from ..database import UserPref

    pref = db.session.get(UserPref, _SECTIONS_PREF_KEY)
    try:
        stored = json.loads(pref.value) if pref and pref.value else None
    except ValueError:
        current_app.logger.warning(
            'Stored account-page section prefs are not valid JSON - using defaults.')
        return None
    return stored if isinstance(stored, dict) and isinstance(stored.get('order'), list) else None


def _effective_settings(account, cfg):
    """Each per-account setting, its effective value, and whether it was inherited.

    An inherited value is labelled `(global)` on the page (DESIGN.md §17.3), so "6h because
    this account says 6h" and "6h because Settings says 6h" are distinguishable at a glance -
    they behave differently the moment the global changes."""
    global_hours = cfg.get('sync', {}).get('sync_interval_hours', config_default('sync.sync_interval_hours'))
    global_conn = cfg.get('accounts', {}).get('default_max_connections', 1)
    mode = resolve_normalization_mode(account, cfg)
    return {
        'interval_hours': account.sync_interval_hours or global_hours,
        'interval_inherited': account.sync_interval_hours is None,
        'max_connections': account.max_connections or global_conn,
        'max_connections_inherited': account.max_connections is None,
        'norm_mode': mode,
        'norm_label': norm_mode_label(mode) or 'Disabled',
        'norm_example': norm_mode_example(mode),
        'norm_inherited': coerce_normalization_mode(account.url_normalization) is None,
    }


def _account_detail_payload(account, cfg, stats):
    """Everything the account page renders, with every lookup batched before any loop.

    Nothing here may run per row - `cfg` is passed in rather than re-read, and the counts
    are aggregate queries, not a walk over the account's channels. `stats` is the page's
    one `section_context()`: the Content card's current numbers and the Usage card both
    come from it, so the catch-up fold and current_stats run once per page."""
    account_ids = [account.id]
    # All time by definition: the Content card says what this account has produced, ever.
    # From the ledger, so each recording is credited only the segments this account
    # captured (dev/changelog/1028) - and so this and the windowed Usage numbers are one
    # source that cannot disagree.
    if stats['window'] == 'all':
        usage_all_time = stats['windowed'][account.id]
    else:
        usage_all_time = windowed_stats(account_ids, 'all', today_local())[account.id]
    rows = _recent_logs_by_account(account_ids, limit=HISTORY_SHOWN).get(account.id, [])
    # Serialized in the SAME shape the syncs API returns, so the history region's first
    # paint and its expand-in-place fetch cannot disagree about what a run looked like.
    # The Activity section renders the ORM rows directly - a different region, server-side,
    # and Jinja's time filters want the datetimes rather than ISO strings.
    logs = [_sync_log_json(log) for log in rows]
    total_syncs = _sync_log_totals(account_ids).get(account.id, 0)
    last_good = next((log for log in rows if log.status == 'SUCCESS'), None)
    # Queried rather than picked out of `rows`: the last ten runs can all be failures, and
    # a failed or cancelled run records no skip counts for the Content card to show.
    last_finished = (AccountSyncLog.query
                     .filter(AccountSyncLog.account_id == account.id,
                             AccountSyncLog.status.in_(('SUCCESS', 'PARTIAL')))
                     .order_by(AccountSyncLog.started_at.desc())
                     .first())
    return {
        'cur': stats['current'][account.id],
        'group_any': GROUP_ANY,
        'all_time_note': account_stats_view.ALL_TIME_NOTE,
        'usage_all_time': usage_all_time,
        'logs': logs,
        'log_rows': rows,
        'total_syncs': total_syncs,
        'has_more_syncs': total_syncs > len(logs),
        'last_good_sync': last_good,
        'last_finished_sync': last_finished,
        'settings': _effective_settings(account, cfg),
        # Which URL form the constructed stream URLs were built in. On the banner because a
        # constructed URL that does not play is nearly always the wrong form, so the form is
        # the first thing to check - it used to be named in the SYNC_STREAM_URLS_CONSTRUCTED
        # alert body, and moved here when that was retired (dev/changelog/928).
        'constructed_mode_label': norm_mode_label(resolve_normalization_mode(account, cfg)),
        # An account's own URL is secret in FULL - for a path-token provider the path IS
        # the credential, and no heuristic can tell (DESIGN-secrets.md §4.2, DESIGN.md §17.4).
        'endpoint': mask_url_path(account.base_url if (account.account_type or 'm3u') == 'xtream'
                                  else account.m3u_url),
        'epg_sources': account_sources_view(account.id, _epg_source_schedule()),
        'source_hours_choices': REFRESH_HOURS_CHOICES,
        # The filtered channel-browser links the new/missing counts point at. These were also
        # the deep-link targets of the SYNC_CHANNELS_NEW/SYNC_CHANNELS_MISSING alerts until
        # dev/changelog/928 retired them - this page is now the only route to them, which is
        # why the links matter more, not less. The account id is fixed to this page's own
        # account, so there is no Alert.source to parse.
        'new_channels_url': url_for('channels.channel_browser',
                                     **{'f.other': OTHER_NEW, 'f.acct': account.id}),
        'removed_channels_url': url_for('channels.channel_browser',
                                         **{'f.other': OTHER_REMOVED, 'f.acct': account.id}),
    }


def _epg_source_schedule() -> dict:
    from ..scheduler import epg_source_schedule
    return epg_source_schedule()


@accounts_bp.route('/accounts/<int:account_id>')
def account_detail(account_id):
    """The per-account page (DESIGN.md §17.3). Its anatomy is the group detail page's -
    an account is a sibling of a group - minus the screenshot frame and the separate
    status strip, both of which §17.3 rules out for an account."""
    try:
        window = account_stats_view.resolve_window(request.args.get('w'))
    except ValueError:
        abort(400, description=f"w must be one of {', '.join(WINDOWS)}")
    cfg = load_config()
    # First, before anything else is loaded: its catch-up fold may commit, which expires
    # every row already in the session. It loads the account too (dev/changelog/1029).
    stats = account_stats_view.section_context(window, cfg, current_app._get_current_object(),
                                               account_ids=[account_id])
    if not stats['accounts']:
        flash('Account not found.', 'error')
        return redirect(url_for('accounts.accounts_list'))
    account = stats['accounts'][0]
    payload = _account_detail_payload(account, cfg, stats)
    return render_template(
        'account_detail.html',
        account=account,
        next_sync_at=next_sync_map([account])[account.id],
        account_type=(account.account_type or 'm3u'),
        preset_colors=PRESET_COLORS,
        norm_options=_NORM_OPTIONS,
        sync_hours_choices=_SYNC_HOURS_CHOICES,
        sections=ACCOUNT_SECTIONS,
        section_pref=_section_pref(),
        # Effective for THIS account - global flag OR its own override - so the kebab
        # menu and the mobile action sheet both light up correctly either way.
        xtream_debug=_xtream_debug_enabled(account),
        stats=stats,
        **payload,
    )


@accounts_bp.route('/accounts/new', methods=['GET', 'POST'])
def new_account():
    cfg = load_config()
    global_sync_hours = cfg.get('sync', {}).get('sync_interval_hours', config_default('sync.sync_interval_hours'))
    global_max_connections = cfg.get('accounts', {}).get('default_max_connections', 1)

    if request.method == 'POST':
        errors = _validate_account_form(request.form)
        xmltv = _form_xmltv_fields(request.form, errors)
        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template(
                'account_form.html',
                account=None,
                form=request.form,
                preset_colors=PRESET_COLORS,
                norm_options=_NORM_OPTIONS,
                norm_example=_norm_example(request.form.get('url_normalization', '')),
                global_sync_hours=global_sync_hours,
                global_max_connections=global_max_connections,
            )

        account_type = request.form.get('account_type', 'm3u')
        dup = _find_duplicate_account(request.form, account_type)
        form = request.form

        # Only the insert is retried, and the retry re-reads nothing that could have
        # changed. Decorating the whole view would put the scheduler call inside the
        # retried unit AND re-run the insert from the top on a lock error, creating a
        # SECOND account row - the exact duplicate-row defect CLAUDE.md's commit rule was
        # written about (it was caught in new_recording_json during that rollout).
        @retry_on_locked()
        def _create():
            sync_hours_raw = _field(form, 'sync_interval_hours')
            max_conn_raw = _field(form, 'max_connections')
            account = Account(
                name=_field(form, 'name'),
                account_type=account_type,
                color=form.get('color', '#58a6ff'),
                url_normalization=coerce_normalization_mode(
                    form.get('url_normalization', '')),
                sync_interval_hours=int(sync_hours_raw) if sync_hours_raw else None,
                sync_enabled=_checkbox(form.get('sync_enabled')),
                max_connections=int(max_conn_raw) if max_conn_raw else None,
                **_type_fields(form, account_type),
            )
            db.session.add(account)
            db.session.flush()
            if account_type == 'xtream':
                ensure_provider_source(account)
            source_id = add_url_source(account, xmltv).id if xmltv else None
            db.session.commit()
            return account.id, account.name, source_id

        account_id, account_name, source_id = _create()

        from ..scheduler import schedule_account_sync
        app_obj = current_app._get_current_object()
        schedule_account_sync(app_obj, account_id)
        if source_id is not None:
            # The source's directory is empty until its first import, so it covers nothing
            # until then - fetch it now rather than at the account's first sync.
            _, refresh_message = start_source_refresh(app_obj, source_id)
            flash(refresh_message, 'info')

        if dup is not None:
            flash(_duplicate_warning(dup, account_type), 'warning')
        flash(f'Account "{account_name}" added. Click Sync Now to fetch channels.', 'success')
        return redirect(url_for('accounts.accounts_list'))

    return render_template(
        'account_form.html',
        account=None,
        form={'sync_enabled': True},
        preset_colors=PRESET_COLORS,
        norm_options=_NORM_OPTIONS,
        norm_example=_norm_example(''),
        global_sync_hours=global_sync_hours,
        global_max_connections=global_max_connections,
    )


def _xtream_debug_enabled(account) -> bool:
    """True if the dump/replay debug tooling should work for this account: either the
    global debug.xtream_debug_mode flag is on, or the account carries its own
    xtream_debug_override (dev/changelog/547 - lets one account under vetting use it
    without putting every account into debug mode)."""
    cfg = load_config()
    if cfg.get('debug', {}).get('xtream_debug_mode', False):
        return True
    return bool(account.xtream_debug_override)


def _require_xtream_debug(account):
    """404 unless debug is enabled for this account (globally or per-account) - the same
    condition that shows the debug buttons in the UI must also gate the routes themselves
    server-side."""
    if not _xtream_debug_enabled(account):
        abort(404)


# ── Actions ───────────────────────────────────────────────────────────────────
#
# One implementation per action, called by every JSON route below. Both Accounts surfaces
# (the list row's kebab and the account page's action bar) drive those routes, so there is
# exactly one implementation of "sync this account" and one message string for it. The
# form-POST routes these helpers were also written for are gone as of DESIGN.md §17: the
# list page no longer posts a form (dev/changelog/456).

def _start_sync(account, *, force=False, force_epg_resync=False):
    """Start a manual sync. Returns (started, message, conflicts).

    `conflicts` is the list of reasons a concurrency clash blocked the start, which the
    caller surfaces individually with a "Sync anyway" affordance - it is a warning the user
    may override, not a refusal (DESIGN-concurrency.md 5.4). It is returned as the list
    rather than a bool because the user is being asked to decide, and "something else is
    running" is not enough to decide on. Empty list means no clash: either the sync started,
    or it failed for a reason force cannot fix. `force_epg_resync` is a separate,
    independently-meaning flag: it bypasses the EPG collapse guard for this one run
    (DESIGN-sync-resilience.md §4), and is enforced here rather than in the UI."""
    from ..accounts import sync_account, sync_conflicts

    if account.status == 'SYNCING':
        return False, f'"{account.name}" is already syncing.', []

    if not force:
        conflicts = sync_conflicts(account.id)
        if conflicts:
            return (False,
                    f'Sync not started for "{account.name}": ' + ' '.join(conflicts),
                    conflicts)

    threading.Thread(
        target=sync_account,
        args=(current_app._get_current_object(), account.id),
        # force_admission: a manual sync is a present user's call, already past the
        # sync_conflicts warn-and-override flow above (DESIGN-concurrency.md §5.4). It
        # still registers, so everything that yields to a sync can see it.
        kwargs={'force_epg_resync': force_epg_resync, 'force_admission': True},
        daemon=True,
        name=f'account-sync-{account.id}',
    ).start()
    return True, f'Sync started for "{account.name}". Refresh in a moment to see results.', []


def _cancel_sync(account):
    """Cancel a running sync, or reset one left stranded by a restart. Returns the message."""
    from ..accounts import cancel_sync

    account_id, name = account.id, account.name
    if cancel_sync(account_id) != 'reset':
        return f'Cancel signal sent for "{name}". Refresh in a moment.'

    @retry_on_locked()
    def _reset():
        finalize_sync_state(
            account_id, 'UNSYNCED', 'CANCELLED',
            account_message='Sync reset (was stuck after service restart)',
            log_message='Reset after service restart',
        )
        db.session.commit()

    _reset()
    return f'Sync state for "{name}" has been reset.'


def _sync_still_running_message(account_id):
    """Why a delete was refused, naming the phase the sync is in when it reports one.

    The phase is what makes the refusal actionable: "still syncing" alone doesn't tell the
    user whether they are waiting seconds or minutes."""
    from ..accounts import get_sync_progress

    account = db.session.get(Account, account_id)
    label = f'"{account.name}"' if account else 'This account'
    phase = (get_sync_progress(account_id) or {}).get('phase')
    where = f' ({phase} phase)' if phase else ''
    return (f'{label} is still syncing{where} and has been told to stop. A sync can only '
            'stop between phases - try the delete again in a moment.')


def _delete_account_and_jobs(account_id):
    """Delete an account and everything the create path scheduled for it.

    Returns (deleted, name_or_reason): the account name on success (None when the row was
    already gone), or the user-facing reason the delete was refused.

    Teardown releases everything the create path acquired: both APScheduler jobs go with
    the row, or a job fires later against an account that no longer exists - and so does a
    running sync thread, which is stopped BEFORE anything else here. A sync mid-flight
    writes channels for this account with no reference to the account row, so tearing the
    row out from under it leaves orphan channels behind (SQLite foreign keys are off, so
    nothing at the database level rejects them). The stop happens ahead of the job removal
    so the refusal path leaves no trace: removing the jobs and then refusing would silently
    disable the account's scheduled sync."""
    from ..accounts import stop_sync_and_wait
    from ..scheduler import get_scheduler, remove_job_if_exists, sync_retry_job_id

    if not stop_sync_and_wait(account_id, 'Cancelled by account delete',
                              _SYNC_STOP_TIMEOUT_SECONDS):
        return False, _sync_still_running_message(account_id)

    # A source refresh has no stop signal to send, and one left running would go on writing
    # rows for sources the delete below removes - refused, like a sync that will not stop.
    owned_sources = EpgSource.query.filter_by(owner_account_id=account_id).all()
    busy = [src.name for src in owned_sources if src.refresh_started_at is not None]
    if busy:
        return False, (f'EPG source "{busy[0]}" is being refreshed right now. Delete the '
                       'account once that has finished.')
    owned_source_ids = [src.id for src in owned_sources]
    source_names = {src.id: src.name for src in owned_sources}
    # Other accounts reading this account's sources, and their channels whose guide comes
    # from one, read while the pointers still exist - the delete clears them
    # (DESIGN-epg-sources.md §9.5, dev/changelog/763). And the sources this account reads
    # from others, which are recounted once it has gone.
    readers = foreign_readers(owned_source_ids, account_id)
    guided_elsewhere = foreign_guided_channels(owned_source_ids, account_id)
    read_elsewhere = [sid for (sid,) in db.session.query(EpgSourceSubscription.source_id)
                      .join(EpgSource, EpgSource.id == EpgSourceSubscription.source_id)
                      .filter(EpgSourceSubscription.account_id == account_id,
                              EpgSource.owner_account_id != account_id)]

    if get_scheduler():
        from ..scheduler import remove_epg_source_jobs
        remove_job_if_exists(f'account_sync_{account_id}')
        remove_job_if_exists(sync_retry_job_id(account_id))
        for sid in owned_source_ids:
            remove_epg_source_jobs(sid)

    # Cached logo files aren't part of the ORM graph (they're bytes on disk, not rows),
    # so the account's own delete-orphan cascade won't touch them - capture the paths
    # before the delete, since they can't be read off the Channel rows once gone
    # (session.expire_on_commit invalidates the attributes after commit() below).
    cached_paths = [
        c.logo_cache_path for c in
        Channel.query.filter_by(account_id=account_id).filter(
            Channel.logo_cache_path.isnot(None)).all()
    ]

    # Which groups are about to lose a member, read while the membership rows still
    # exist - the account's cascade takes its channels, and Channel.group_memberships'
    # delete-orphan cascade takes their memberships with them, so this query finds
    # nothing once the commit below has landed. A group left in the TV Guide with
    # nothing switched on for recording is DESIGN-channel-groups-model.md 15's breach
    # path 3 with nobody present to confirm it: it gets an alert afterward and keeps its
    # row, exactly as the missing-channel bulk delete handles it (dev/changelog/763).
    touched_group_ids = {
        gid for (gid,) in
        db.session.query(ChannelGroupMember.group_id).join(
            Channel, Channel.id == ChannelGroupMember.channel_id).filter(
            Channel.account_id == account_id).distinct().all()
    }

    @retry_on_locked()
    def _delete():
        account = db.session.get(Account, account_id)
        if account is None:
            return None
        name = account.name
        # Not in the ORM cascade: the ledger has no relationship on Account, and a day row
        # left behind would credit a future account that reused the id.
        AccountStatDay.query.filter_by(account_id=account_id).delete(synchronize_session=False)
        # Nor are its EPG sources, which may hold hundreds of thousands of rows across two
        # tables and are bulk-deleted rather than cascaded (DESIGN-epg-sources.md §9.5).
        delete_sources_for_account(account_id)
        db.session.delete(account)
        db.session.commit()
        return name

    name = _delete()
    for sid in owned_source_ids:
        resolve_source_alerts(sid)
    outcome = (reresolve_channels(list(guided_elsewhere), _case_sensitive(),
                                  previous=guided_elsewhere, source_names=source_names)
               if guided_elsewhere else {})
    for sid in owned_source_ids:
        report_source_removed(sid, source_names[sid],
                              f'with its account "{name}"' if name else 'with its account',
                              readers, guided_elsewhere, outcome)
    if read_elsewhere:
        forget_reader(read_elsewhere)
    if cached_paths:
        delete_cached_logos(cached_paths)
    report_orphaned_guide_groups(
        touched_group_ids,
        cause=f'The channels it could record from belonged to the deleted account "{name}".'
        if name else 'The channels it could record from belonged to a deleted account.')
    return True, name


def _dump_account(account_id):
    """Debug-only: fetch every Xtream API response to files. Returns (ok, message)."""
    from ..xtream_client import dump_xtream_account
    try:
        dump_dir = dump_xtream_account(current_app._get_current_object(), account_id)
    except Exception as exc:  # noqa: BLE001 - the provider or the filesystem; both are reported
        current_app.logger.warning('Xtream dump failed for account %s: %s', account_id, exc)
        return False, f'Dump failed: {exc}'
    return True, f'Xtream data dumped to: {dump_dir}'


def _start_sync_from_dump(account):
    """Debug-only: sync from previously dumped files. Returns (started, message)."""
    from ..accounts import sync_account

    if account.status == 'SYNCING':
        return False, f'"{account.name}" is already syncing.'
    threading.Thread(
        target=sync_account,
        args=(current_app._get_current_object(), account.id),
        kwargs={'use_dump': True, 'force_admission': True},
        daemon=True,
        name=f'account-sync-dump-{account.id}',
    ).start()
    return True, (f'Sync from dump started for "{account.name}". '
                  'Refresh in a moment to see results.')


def _save_account_settings(account_id, data):
    """Validate and persist an account edit. Returns (errors, duplicate_or_None, name).

    `data` is a form-like mapping - a werkzeug MultiDict from the page, or a plain dict
    from the JSON API - so both go through the same validation and the same field
    coercion. The scheduler reschedule is a non-idempotent side effect and therefore sits
    OUTSIDE the retried closure (CLAUDE.md)."""
    errors = _validate_account_form(data)
    if errors:
        return errors, None, None

    account_type = data.get('account_type', 'm3u')
    dup = _find_duplicate_account(data, account_type, exclude_id=account_id)

    @retry_on_locked()
    def _save():
        account = db.session.get(Account, account_id)
        account.name = data['name'].strip()
        account.account_type = account_type
        account.color = data.get('color') or account.color
        account.url_normalization = coerce_normalization_mode(
            data.get('url_normalization', ''))
        sync_hours_raw = str(data.get('sync_interval_hours', '') or '').strip()
        account.sync_interval_hours = int(sync_hours_raw) if sync_hours_raw else None
        account.sync_enabled = _checkbox(data.get('sync_enabled'))
        max_conn_raw = str(data.get('max_connections', '') or '').strip()
        account.max_connections = int(max_conn_raw) if max_conn_raw else None
        account.xtream_debug_override = _checkbox(data.get('xtream_debug_override'))
        for k, v in _type_fields(data, account_type, existing=account).items():
            setattr(account, k, v)
        account.updated_at = datetime.utcnow()
        db.session.commit()
        return account.name

    name = _save()
    from ..scheduler import schedule_account_sync
    schedule_account_sync(current_app._get_current_object(), account_id, force_reschedule=True)
    return [], dup, name


def _checkbox(value) -> bool:
    """One reading of "the user ticked this", whether it arrived as an HTML checkbox
    ('on' / absent) or as JSON (a real bool). A missing key is False either way."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('on', 'true', '1', 'yes')


# ── JSON API ──────────────────────────────────────────────────────────────────
#
# The account page drives every action through these; part 2's list page will use the same
# ones. Standard envelope throughout: {'success': True, ...} or {'error': '<message>'} with
# a real status code. CSRF is app-wide, so nothing is exempt here.

def _json_account(account_id):
    """(account, error_response). The error response is already jsonify'd."""
    account = db.session.get(Account, account_id)
    if account is None:
        return None, (jsonify({'error': 'Account not found.'}), 404)
    return account, None


@accounts_bp.route('/api/accounts/<int:account_id>', methods=['GET'])
def account_settings_api(account_id):
    """The edit modal's field values.

    The password is deliberately absent: it is never served back out, in any form. The
    modal shows a blank field with "leave blank to keep existing" and an eyeball that only
    reveals what is being typed right now (DESIGN.md §17.4) - this is narrower than
    §3.16's config-secret reveal on purpose, because a provider's password is not ours to
    hand out again."""
    account, err = _json_account(account_id)
    if err:
        return err
    cfg = load_config()
    return jsonify({
        'success': True,
        'account': {
            'id': account.id,
            'name': account.name,
            'account_type': account.account_type or 'm3u',
            'm3u_url': account.m3u_url or '',
            'base_url': account.base_url or '',
            'username': account.username or '',
            'has_password': bool(account.password),
            'color': account.color,
            'url_normalization': coerce_normalization_mode(account.url_normalization) or '',
            'sync_interval_hours': account.sync_interval_hours or '',
            'sync_enabled': bool(account.sync_enabled),
            'max_connections': account.max_connections or '',
            'xtream_debug_override': bool(account.xtream_debug_override),
        },
        'norm_options': [{'value': v, 'label': label, 'example': example}
                         for v, label, example in _NORM_OPTIONS],
        'sync_hours_choices': _SYNC_HOURS_CHOICES,
        'preset_colors': [{'hex': hexval, 'label': label} for hexval, label in PRESET_COLORS],
        'global_sync_hours': cfg.get('sync', {}).get('sync_interval_hours', config_default('sync.sync_interval_hours')),
        'global_max_connections': cfg.get('accounts', {}).get('default_max_connections', 1),
        'global_xtream_debug': cfg.get('debug', {}).get('xtream_debug_mode', False),
    })


@accounts_bp.route('/api/accounts/<int:account_id>', methods=['POST'])
def save_account_api(account_id):
    account, err = _json_account(account_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    errors, dup, name = _save_account_settings(account_id, data)
    if errors:
        return jsonify({'error': ' '.join(errors)}), 400
    return jsonify({
        'success': True,
        'name': name,
        'message': f'Account "{name}" updated.',
        # Advisory, never blocking - the same warning the form page flashes.
        'warning': _duplicate_warning(dup, data.get('account_type', 'm3u')) if dup else None,
    })


@retry_on_locked()
def _renormalize_chunk(chunk_ids, mode):
    """Recompute stream_url/url_normalizable for one chunk of channels from their stored
    raw_stream_url, under `mode`. Deterministic and idempotent (never inserts a row), so a
    lock-retry re-running the whole chunk is always safe - unlike an INSERT, there is no
    duplicate-row risk from CLAUDE.md's "one commit per decorated closure" rule."""
    rows = Channel.query.filter(Channel.id.in_(chunk_ids)).all()
    mappings = []
    stamped_at = datetime.utcnow()
    for ch in rows:
        if not ch.raw_stream_url:
            continue
        new_url = normalize_url_with_mode(ch.raw_stream_url, mode)
        normalizable = url_is_normalizable(ch.raw_stream_url)
        if new_url != ch.stream_url or normalizable != ch.url_normalizable:
            row = {'id': ch.id, 'stream_url': new_url, 'url_normalizable': normalizable}
            if new_url != ch.stream_url:
                # stream_url is one of the four columns ch_fts indexes, so rewriting it
                # genuinely moves the channels search index's source and has to stamp the
                # watermark (app/database.py::Channel.search_text_updated_at). This is the
                # only writer of an indexed column outside _upsert_channels; url_normalizable
                # is not indexed and must not stamp on its own.
                row['search_text_updated_at'] = stamped_at
            mappings.append(row)
    if mappings:
        db.session.bulk_update_mappings(Channel, mappings)
    db.session.commit()
    return len(mappings)


@accounts_bp.route('/api/accounts/<int:account_id>/renormalize-urls', methods=['POST'])
def renormalize_urls_api(account_id):
    """Rewrite every existing channel's stream_url from its stored raw_stream_url under
    this account's CURRENT (already-saved) URL Normalization mode - no provider connection.
    Paired with the URL Normalization field in the edit-account modal (pair it with the
    action that creates the need for it, 2026-08-04) - account-modal.js saves the mode
    first, then calls this, so "current mode" here is always the mode just chosen."""
    account, err = _json_account(account_id)
    if err:
        return err
    cfg = load_config()
    mode = resolve_normalization_mode(account, cfg)
    if mode == NORM_DISABLED:
        return jsonify({'error': 'URL Normalization is Disabled for this account (and for '
                                  'the global default) - choose a mode first, then '
                                  're-normalize.'}), 400

    ids = [cid for (cid,) in db.session.query(Channel.id)
           .filter_by(account_id=account_id).all()]
    changed = 0
    for chunk in _chunked(ids, _DELETE_CHUNK_SIZE):
        changed += _renormalize_chunk(chunk, mode)
    if changed:
        recompute_duplicate_stream_urls_and_commit()

    label = norm_mode_label(mode) or 'the global default'
    message = (f'Re-normalized {changed} of {len(ids)} channel URL(s) to {label}.' if changed
               else f'All {len(ids)} channel URL(s) already match {label}.')
    return jsonify({
        'success': True,
        'changed_count': changed,
        'total_count': len(ids),
        'message': message,
    })


@accounts_bp.route('/api/accounts/<int:account_id>', methods=['DELETE'])
def delete_account_api(account_id):
    account, err = _json_account(account_id)
    if err:
        return err
    deleted, name = _delete_account_and_jobs(account_id)
    # 409, not 400: a sync that hasn't stopped yet is a state clash the caller can resolve
    # by retrying, not a malformed request.
    if not deleted:
        return jsonify({'error': name}), 409
    # None means the row went between the lookup above and the retried delete closure - a
    # concurrent delete, not an error worth surfacing, since the outcome the caller asked
    # for is the outcome they got.
    if name is None:
        return jsonify({'success': True, 'message': 'Account deleted.'})
    return jsonify({'success': True, 'message': f'Account "{name}" deleted.'})


@accounts_bp.route('/api/accounts/<int:account_id>/sync', methods=['POST'])
def sync_account_api(account_id):
    account, err = _json_account(account_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    started, message, conflicts = _start_sync(
        account,
        force=_checkbox(data.get('force')),
        force_epg_resync=_checkbox(data.get('force_epg_resync')),
    )
    if not started:
        # 409 for the concurrency conflict: it is a state clash the user can override by
        # re-sending with force, not a malformed request. The reasons ride along as a LIST
        # (the shape routes/jobs.py::run_job_now already returns) because the caller has to
        # name each one in its override confirm - DESIGN-concurrency.md 5.4 is warn-and-
        # override, and a caller that only knows "conflict: true" can only refuse.
        if conflicts:
            return jsonify({'error': message, 'conflict': True, 'conflicts': conflicts}), 409
        return jsonify({'error': message, 'conflict': False, 'conflicts': []}), 400
    return jsonify({'success': True, 'message': message})


@accounts_bp.route('/api/accounts/<int:account_id>/sync/cancel', methods=['POST'])
def cancel_sync_api(account_id):
    account, err = _json_account(account_id)
    if err:
        return err
    return jsonify({'success': True, 'message': _cancel_sync(account)})


# ── EPG sources on the account page (DESIGN-epg-sources.md §9.2) ──────────────────

def _json_owned_source(account_id, source_id, *, url_only=True):
    """(account, source, error_response) for a source this account owns. Adding, editing,
    deleting and refreshing are the owner's (§3); a provider source is edited only by
    editing its account."""
    account, err = _json_account(account_id)
    if err:
        return None, None, err
    source = db.session.get(EpgSource, source_id)
    if source is None or source.owner_account_id != account_id:
        return None, None, (jsonify({'error': 'EPG source not found on this account.'}), 404)
    if url_only and source.kind != EPG_SOURCE_URL:
        return None, None, (jsonify({'error': "The provider's guide comes with the account - "
                                              'edit the account instead.'}), 400)
    return account, source, None


def _case_sensitive() -> bool:
    return load_config().get('sync', {}).get('epg_case_sensitive_matching', False)


def _removal_message(name: str, done, verb: str) -> str:
    if not done.affected:
        return f'{verb} "{name}". No channel on this account was getting its guide from it.'
    parts = []
    if done.switched:
        parts.append(f'{done.switched:,} now take their guide from another source')
    if done.lost:
        parts.append(f'{done.lost:,} have no guide from any source now')
    return (f'{verb} "{name}". {done.affected:,} channel(s) were getting their guide from it: '
            + ', and '.join(parts) + '.')


@accounts_bp.route('/api/accounts/<int:account_id>/epg-sources', methods=['POST'])
def add_epg_source_api(account_id):
    """Add source (URL). Subscribed last, then fetched at once in the background: until
    its first import its directory is empty and it covers nothing (§12.1)."""
    account, err = _json_account(account_id)
    if err:
        return err
    errors, fields = clean_url_source_fields(request.get_json(silent=True) or {}, account.name)
    if errors:
        return jsonify({'error': ' '.join(errors)}), 400

    @retry_on_locked()
    def _add_and_commit():
        source = add_url_source(db.session.get(Account, account_id), fields)
        db.session.commit()
        return source.id

    source_id = _add_and_commit()
    app_obj = current_app._get_current_object()
    from ..scheduler import schedule_epg_source_refresh
    started, message = start_source_refresh(app_obj, source_id)
    if fields['refresh_interval_hours']:
        schedule_epg_source_refresh(
            app_obj, source_id,
            first_run=datetime.utcnow() + timedelta(hours=fields['refresh_interval_hours']))
    return jsonify({'success': True, 'source_id': source_id, 'refresh_started': started,
                    'message': f'Added "{fields["name"]}". {message}'})


@accounts_bp.route('/api/accounts/<int:account_id>/epg-sources/<int:source_id>',
                   methods=['GET'])
def epg_source_settings_api(account_id, source_id):
    """The Edit source dialog's values. The URL is served in full, as the account's own
    URLs are to the edit-account modal: this is the owner's settings dialog, and a masked
    value saved back would replace the real one."""
    _account, source, err = _json_owned_source(account_id, source_id)
    if err:
        return err
    return jsonify({'success': True, 'source': {
        'id': source.id, 'name': source.name, 'url': source.url or '',
        'refresh_interval_hours': source.refresh_interval_hours or '',
        'enabled': bool(source.enabled)}})


@accounts_bp.route('/api/accounts/<int:account_id>/epg-sources/<int:source_id>',
                   methods=['POST'])
def save_epg_source_api(account_id, source_id):
    account, source, err = _json_owned_source(account_id, source_id)
    if err:
        return err
    errors, fields = clean_url_source_fields(request.get_json(silent=True) or {}, account.name)
    if errors:
        return jsonify({'error': ' '.join(errors)}), 400

    @retry_on_locked()
    def _save_and_commit():
        changed = update_url_source(db.session.get(EpgSource, source_id), fields)
        db.session.commit()
        return changed

    url_changed = _save_and_commit()
    update_source_stale_alert(source_id)
    app_obj = current_app._get_current_object()
    from ..scheduler import schedule_epg_source_refresh
    message = f'Saved "{fields["name"]}".'
    if url_changed and fields['enabled']:
        # A new URL is a different file, and the directory describes the old one.
        message += ' ' + start_source_refresh(app_obj, source_id)[1]
        first = (datetime.utcnow() + timedelta(hours=fields['refresh_interval_hours'])
                 if fields['refresh_interval_hours'] else None)
        schedule_epg_source_refresh(app_obj, source_id, force_reschedule=True, first_run=first)
    else:
        schedule_epg_source_refresh(app_obj, source_id, force_reschedule=True)
    return jsonify({'success': True, 'message': message})


@accounts_bp.route('/api/accounts/<int:account_id>/epg-sources/<int:source_id>',
                   methods=['DELETE'])
def delete_epg_source_api(account_id, source_id):
    """Delete a url source (§9.5): its listings, directory, keys and jobs, and every
    channel it was the guide for moves to its next source. Refused while it is being
    refreshed - the import would go on writing rows for a source that no longer exists."""
    _account, source, err = _json_owned_source(account_id, source_id)
    if err:
        return err
    if source.refresh_started_at is not None:
        return jsonify({'error': f'"{source.name}" is being refreshed right now. Delete it '
                                 'once the refresh has finished.'}), 409
    name = source.name
    readers = foreign_readers([source_id], account_id)
    guided_elsewhere = foreign_guided_channels([source_id], account_id)
    from ..scheduler import remove_epg_source_jobs
    remove_epg_source_jobs(source_id)
    done = delete_url_source(source_id, _case_sensitive())
    resolve_source_alerts(source_id)
    if readers:
        outcome = dict(db.session.query(Channel.id, Channel.epg_source_id).filter(
            Channel.id.in_(list(guided_elsewhere)))) if guided_elsewhere else {}
        report_source_removed(source_id, name, f'by hand on "{_account.name}"', readers,
                              guided_elsewhere, outcome)
    if done is None:
        return jsonify({'success': True, 'message': 'EPG source deleted.'})
    return jsonify({'success': True, 'message': _removal_message(name, done, 'Deleted')})


@accounts_bp.route('/api/accounts/<int:account_id>/epg-sources/<int:source_id>/refresh',
                   methods=['POST'])
def refresh_epg_source_api(account_id, source_id):
    _account, source, err = _json_owned_source(account_id, source_id)
    if err:
        return err
    if not source.enabled:
        return jsonify({'error': f'"{source.name}" is turned off. Turn it on in Edit '
                                 'first.'}), 400
    if source.refresh_started_at is not None:
        return jsonify({'error': f'"{source.name}" is already being refreshed.'}), 409
    started, message = start_source_refresh(current_app._get_current_object(), source_id)
    return jsonify({'success': True, 'refresh_started': started, 'message': message})


@accounts_bp.route('/api/accounts/<int:account_id>/epg-sources/<int:source_id>/stop-using',
                   methods=['POST'])
def stop_using_epg_source_api(account_id, source_id):
    """Stop reading a source (§3). Offered for the provider's guide; a url source is
    deleted instead, since nothing else can read it yet."""
    account, err = _json_account(account_id)
    if err:
        return err
    source = db.session.get(EpgSource, source_id)
    if source is None:
        return jsonify({'error': 'EPG source not found.'}), 404
    if source.refresh_started_at is not None:
        return jsonify({'error': f'"{source.name}" is being refreshed right now. Try again '
                                 'once the refresh has finished.'}), 409
    done = stop_using_source(account_id, source_id, _case_sensitive())
    if done is None:
        return jsonify({'error': 'This account is not using that source.'}), 400
    from ..scheduler import schedule_epg_source_refresh
    schedule_epg_source_refresh(current_app._get_current_object(), source_id,
                                force_reschedule=True)
    return jsonify({'success': True,
                    'message': _removal_message(source.name, done, 'Stopped using')})


@accounts_bp.route('/api/accounts/<int:account_id>/epg-sources/<int:source_id>/use-again',
                   methods=['POST'])
def use_epg_source_again_api(account_id, source_id):
    """Undo Stop using. The source's listings come back at its next refresh - for the
    provider's guide that is the account's next sync; nothing is fetched here, since that
    fetch is a provider connection and Sync now is the control that makes one."""
    _account, source, err = _json_owned_source(account_id, source_id, url_only=False)
    if err:
        return err

    @retry_on_locked()
    def _subscribe_and_commit():
        added = use_source_again(account_id, source_id)
        db.session.commit()
        return added

    if not _subscribe_and_commit():
        return jsonify({'error': 'This account already uses that source.'}), 400
    from ..scheduler import schedule_epg_source_refresh
    schedule_epg_source_refresh(current_app._get_current_object(), source_id,
                                force_reschedule=True)
    return jsonify({'success': True,
                    'message': f'Using "{source.name}" again. Its listings come back at its '
                               'next refresh - Sync now fetches it now.'})


@accounts_bp.route('/api/accounts/<int:account_id>/epg-sources/<int:source_id>/move',
                   methods=['POST'])
def move_epg_source_api(account_id, source_id):
    """Body {direction: 'up' | 'down'}: one place in the account's priority order
    (DESIGN-epg-sources.md §5.2). Every channel's guide is re-decided from listings already
    held; nothing is fetched."""
    _account, err = _json_account(account_id)
    if err:
        return err
    direction = (request.get_json(silent=True) or {}).get('direction')
    if direction not in ('up', 'down'):
        return jsonify({'error': 'direction must be "up" or "down"'}), 400
    # An import resolving these channels at the same time would race the moves.
    busy = [name for (name,) in db.session.query(EpgSource.name)
            .join(EpgSourceSubscription, EpgSourceSubscription.source_id == EpgSource.id)
            .filter(EpgSourceSubscription.account_id == account_id,
                    EpgSource.refresh_started_at.isnot(None))]
    if busy:
        return jsonify({'error': f'"{busy[0]}" is being refreshed right now. Try again once '
                                 'the refresh has finished.'}), 409
    done = move_source(account_id, source_id, -1 if direction == 'up' else 1,
                       _case_sensitive())
    if done is None:
        return jsonify({'error': 'This account is not using that source.'}), 400
    name = db.session.get(EpgSource, source_id).name
    if not done.moved:
        return jsonify({'success': True, 'message': f'"{name}" is already '
                        f'{"first" if direction == "up" else "last"}.'})
    if done.switched == 1:
        switched = '1 channel now takes its guide from a different source.'
    elif done.switched:
        switched = f'{done.switched:,} channels now take their guide from a different source.'
    else:
        switched = 'No channel changed where its guide comes from.'
    return jsonify({'success': True, 'switched': done.switched,
                    'message': f'Moved "{name}" {direction}. {switched}'})


@accounts_bp.route('/api/accounts/<int:account_id>/epg-sources/borrowable', methods=['GET'])
def borrowable_epg_sources_api(account_id):
    """Add source's "another account's guide" list: every source another account owns that
    this one does not read, with what reading it would do to this account's channels today
    (epg_sources.borrowable_sources). Only the owner's refresh ever fetches it."""
    _account, err = _json_account(account_id)
    if err:
        return err
    tz, h24 = get_display_tz(), is_24h()
    sources = []
    for s in borrowable_sources(account_id, _case_sensitive()):
        s['last_success_at'] = (format_local(s['last_success_at'], tz=tz, h24=h24)
                                if s['last_success_at'] else None)
        sources.append(s)
    return jsonify({'success': True, 'sources': sources})


def _borrowed_message(source, owner_name: str, done) -> str:
    """What subscribing did, in the order the user asks it: what has a guide now, what is
    still to come, and when."""
    when = (f'at "{owner_name}"\'s next sync' if source.kind == EPG_SOURCE_PROVIDER
            else 'at its next refresh')
    parts = [f'Now using "{source.name}" from "{owner_name}".']
    if done.turned_on:
        parts.append('Nobody was reading it, so it had stopped being downloaded; it is '
                     'downloaded again from now on.')
    if not done.refreshed:
        parts.append(f'It has no successful refresh yet, so which channels it covers is not '
                     f'known - its listings arrive {when}.')
    elif not done.covered:
        parts.append('Its last refresh has no listings for any channel on this account.')
    else:
        if done.copied:
            parts.append(f'{done.copied:,} channel(s) got its listings right away, and '
                         f'{done.guided:,} of them now take their guide from it.')
        if done.waiting:
            parts.append(f'{done.waiting:,} more it covers get their listings {when}.')
    if source.kind == EPG_SOURCE_URL and not source.enabled:
        parts.append(f'It is turned off on "{owner_name}", so it is not being refreshed.')
    return ' '.join(parts)


@accounts_bp.route('/api/accounts/<int:account_id>/epg-sources/subscribe', methods=['POST'])
def subscribe_epg_source_api(account_id):
    """Read another account's source (DESIGN-epg-sources.md §3). Last in this account's
    priority order, so it only gives a guide where nothing above it covers the channel;
    what the database already holds is copied at once and nothing is fetched."""
    _account, err = _json_account(account_id)
    if err:
        return err
    source_id = (request.get_json(silent=True) or {}).get('source_id')
    source = db.session.get(EpgSource, source_id) if isinstance(source_id, int) else None
    if source is None:
        return jsonify({'error': 'EPG source not found.'}), 404
    if source.owner_account_id == account_id:
        return jsonify({'error': 'That source belongs to this account - use Use again on '
                                 'its row instead.'}), 400
    if source.refresh_started_at is not None:
        return jsonify({'error': f'"{source.name}" is being refreshed right now. Try again '
                                 'once the refresh has finished.'}), 409
    owner_name = source.owner.name if source.owner else 'its account'
    done = subscribe_to_source(account_id, source.id, _case_sensitive())
    if done is None:
        return jsonify({'error': 'This account already uses that source.'}), 400
    from ..scheduler import schedule_epg_source_refresh
    schedule_epg_source_refresh(current_app._get_current_object(), source.id,
                                force_reschedule=True)
    source = db.session.get(EpgSource, source.id)
    return jsonify({'success': True, 'message': _borrowed_message(source, owner_name, done)})


@accounts_bp.route('/api/accounts/<int:account_id>/epg-readers', methods=['GET'])
def epg_readers_api(account_id):
    """Other accounts reading this account's EPG sources, for the delete confirm's second
    sentence (DESIGN-epg-sources.md §9.5)."""
    _account, err = _json_account(account_id)
    if err:
        return err
    owned = [sid for (sid,) in db.session.query(EpgSource.id)
             .filter(EpgSource.owner_account_id == account_id)]
    totals: dict[tuple[str, int], int] = {}
    for fr in foreign_readers(owned, account_id):
        key = (fr.account_name, fr.account_id)
        totals[key] = totals.get(key, 0) + fr.guided
    return jsonify({'success': True, 'readers': [
        {'id': aid, 'name': n, 'guided': g} for (n, aid), g in sorted(totals.items())]})


# ── Name matching review (DESIGN-epg-sources.md §7.5, dev/changelog/1105) ─────────

_REVIEW_PER_PAGE = 100
_REVIEW_VIEWS = ('proposals', 'disagreements', 'rejected')


def _upcoming_view(entry, tz, h24) -> list[dict]:
    out = []
    for start, title in entry.upcoming or ():
        try:
            when = datetime.fromisoformat(start)
        except (TypeError, ValueError):
            continue
        out.append({'at': format_local(when, 'monthday_time', tz=tz, h24=h24), 'title': title})
    return out


def _file_row_view(entry, tz, h24) -> dict:
    return {'xml_id': entry.xml_id, 'entry_count': entry.entry_count,
            'distinct_titles': entry.distinct_titles, 'sole_title': entry.sole_title,
            'horizon': format_local(entry.horizon_until, 'monthday_time', tz=tz, h24=h24)
            if entry.horizon_until else None,
            'upcoming': _upcoming_view(entry, tz, h24)}


@accounts_bp.route('/epg-sources/<int:source_id>/review')
def epg_source_review(source_id):
    """Name matches a source's file proposes for the channels reading it (§7.5). Nothing
    here is applied until a person accepts it; the provider's id stays the match for
    every channel nobody reviews. A fixed number of queries whatever the channel count -
    proposals are computed in Python over one directory read and one channel read."""
    source = db.session.get(EpgSource, source_id)
    if source is None:
        abort(404)
    view = request.args.get('view', 'proposals')
    if view not in _REVIEW_VIEWS:
        view = 'proposals'
    include_single = request.args.get('single') == '1'
    unguided_only = request.args.get('unguided') == '1'
    page = max(request.args.get('page', 1, type=int) or 1, 1)

    result = name_match_review(source, _case_sensitive(), include_single)
    new = [p for p in result.proposals if p.kind == MATCH_NEW]
    disagree = [p for p in result.proposals if p.kind == MATCH_DISAGREE]
    if unguided_only:
        new = [p for p in new if p.channel.epg_source_id is None]
    rejected = rejected_name_matches(source_id)
    listed = {'proposals': new, 'disagreements': disagree, 'rejected': rejected}[view]
    pages = max((len(listed) + _REVIEW_PER_PAGE - 1) // _REVIEW_PER_PAGE, 1)
    page = min(page, pages)
    shown = listed[(page - 1) * _REVIEW_PER_PAGE:page * _REVIEW_PER_PAGE]

    account_names = dict(db.session.query(Account.id, Account.name))
    source_names = dict(db.session.query(EpgSource.id, EpgSource.name))
    tz, h24 = get_display_tz(), is_24h()
    rows = []
    for item in shown:
        if view == 'rejected':
            rows.append({**item, 'account_name': account_names.get(item['account_id'], '?'),
                         'rejected_at': format_local(item['rejected_at'], 'monthday_time',
                                                     tz=tz, h24=h24)})
            continue
        ch = item.channel
        row = {'channel_id': ch.id, 'channel_name': ch.name, 'account_id': ch.account_id,
               'account_name': account_names.get(ch.account_id, '?'),
               'provider_id': ch.epg_channel_id,
               'guide_from': source_names.get(ch.epg_source_id) if ch.epg_source_id else None,
               'matched_on': item.matched_on, 'file': _file_row_view(item.entry, tz, h24),
               'current': _file_row_view(item.current, tz, h24) if item.current else None}
        row['same_listings'] = bool(
            row['current'] and row['file']['upcoming']
            and [u['title'] for u in row['file']['upcoming']]
            == [u['title'] for u in row['current']['upcoming']])
        rows.append(row)

    owner = db.session.get(Account, source.owner_account_id)
    has_upcoming = any(r.get('file', {}).get('upcoming') for r in rows)
    return render_template(
        'epg_source_review.html', source=source, owner=owner, view=view, rows=rows,
        page=page, pages=pages, total=len(listed), per_page=_REVIEW_PER_PAGE,
        counts={'proposals': len(new), 'disagreements': len(disagree),
                'rejected': len(rejected)},
        ambiguous=result.ambiguous, single_hidden=result.single_hidden,
        include_single=include_single, unguided_only=unguided_only,
        refreshed=has_directory(source.id), has_upcoming=has_upcoming,
        can_refresh=source.kind == EPG_SOURCE_URL and source.enabled)


def _review_picks(data) -> list[tuple[int, str]]:
    picks = []
    for p in data.get('picks') or []:
        try:
            picks.append((int(p['channel_id']), str(p['xml_id'])))
        except (KeyError, TypeError, ValueError):
            continue
    return picks


def _review_source_or_error(source_id):
    source = db.session.get(EpgSource, source_id)
    if source is None:
        return None, (jsonify({'error': 'EPG source not found.'}), 404)
    return source, None


@accounts_bp.route('/api/epg-sources/<int:source_id>/name-matches/accept', methods=['POST'])
def accept_name_matches_api(source_id):
    source, err = _review_source_or_error(source_id)
    if err:
        return err
    picks = _review_picks(request.get_json(silent=True) or {})
    if not picks:
        return jsonify({'error': 'Nothing selected.'}), 400
    done = accept_name_matches(source, picks, _case_sensitive())
    parts = [f'Accepted {done.applied:,} name match{"es" if done.applied != 1 else ""}.']
    if done.waiting:
        when = ('at its next refresh - Refresh now fetches it now'
                if source.kind == EPG_SOURCE_URL else "at the account's next sync")
        parts.append(f'{done.waiting:,} get their listings from "{source.name}" {when}.')
    if done.stale:
        parts.append(f'{done.stale:,} were no longer proposed and were left alone.')
    return jsonify({'success': True, 'accepted': done.applied, 'waiting': done.waiting,
                    'stale': done.stale, 'message': ' '.join(parts)})


@accounts_bp.route('/api/epg-sources/<int:source_id>/name-matches/reject', methods=['POST'])
def reject_name_matches_api(source_id):
    source, err = _review_source_or_error(source_id)
    if err:
        return err
    picks = _review_picks(request.get_json(silent=True) or {})
    if not picks:
        return jsonify({'error': 'Nothing selected.'}), 400
    done = reject_name_matches(source, picks, _case_sensitive())
    msg = f'Rejected {done.applied:,}. They will not be proposed again.'
    if done.stale:
        msg += f' {done.stale:,} were no longer proposed and were left alone.'
    return jsonify({'success': True, 'rejected': done.applied, 'stale': done.stale,
                    'message': msg})


@accounts_bp.route('/api/epg-sources/<int:source_id>/name-matches/propose-again',
                   methods=['POST'])
def propose_again_api(source_id):
    _source, err = _review_source_or_error(source_id)
    if err:
        return err
    ids = []
    for cid in (request.get_json(silent=True) or {}).get('channel_ids') or []:
        try:
            ids.append(int(cid))
        except (TypeError, ValueError):
            continue
    if not ids:
        return jsonify({'error': 'Nothing selected.'}), 400
    n = propose_again(source_id, ids)
    return jsonify({'success': True, 'count': n,
                    'message': f'{n:,} channel(s) will be proposed again if their names '
                               'still match.'})


@accounts_bp.route('/api/accounts/<int:account_id>/dump', methods=['POST'])
def dump_account_api(account_id):
    account, err = _json_account(account_id)
    if err:
        return err
    _require_xtream_debug(account)
    if (account.account_type or 'm3u') != 'xtream':
        return jsonify({'error': 'Not an Xtream account.'}), 400
    ok, message = _dump_account(account_id)
    if not ok:
        return jsonify({'error': message}), 400
    return jsonify({'success': True, 'message': message})


@accounts_bp.route('/api/accounts/<int:account_id>/sync-from-dump', methods=['POST'])
def sync_from_dump_api(account_id):
    account, err = _json_account(account_id)
    if err:
        return err
    _require_xtream_debug(account)
    if (account.account_type or 'm3u') != 'xtream':
        return jsonify({'error': 'Not an Xtream account.'}), 400
    started, message = _start_sync_from_dump(account)
    if not started:
        return jsonify({'error': message}), 409
    return jsonify({'success': True, 'message': message})


@accounts_bp.route('/api/accounts/<int:account_id>/syncs', methods=['GET'])
def account_syncs_api(account_id):
    """Every sync run for this account, for the history section's expand-in-place control.

    There is no separate history page: DESIGN.md §17.1 retired account_logs.html, and the
    "All N syncs" button loads the rest into the section it already lives in."""
    account, err = _json_account(account_id)
    if err:
        return err
    logs = (AccountSyncLog.query
            .filter_by(account_id=account_id)
            .order_by(AccountSyncLog.started_at.desc())
            .all())
    return jsonify({
        'success': True,
        'total': len(logs),
        'logs': [_sync_log_json(log) for log in logs],
    })


# ── Helpers ───────────────────────────────────────────────────────────────────

def _field(form, key: str) -> str:
    """One stripped string reading of a submitted field.

    The same mappings arrive from two places - a werkzeug MultiDict, where every value is
    a string, and a JSON body, where `max_connections` may be a real int and an omitted
    field may be None. Coercing here is what lets both paths share one validator instead
    of growing a second, subtly different copy."""
    value = form.get(key)
    return '' if value is None else str(value).strip()


def _validate_account_form(form) -> list[str]:
    errors = []
    if not _field(form, 'name'):
        errors.append('Name is required.')
    account_type = form.get('account_type', 'm3u')
    if account_type == 'm3u':
        if not _field(form, 'm3u_url'):
            errors.append('M3U URL is required.')
    else:
        if not _field(form, 'base_url'):
            errors.append('Base URL is required.')
        if not _field(form, 'username'):
            errors.append('Username is required.')
    max_conn_raw = _field(form, 'max_connections')
    if max_conn_raw and not (max_conn_raw.isdigit() and int(max_conn_raw) >= 1):
        errors.append('Max connections must be a positive whole number.')
    sync_hours_raw = _field(form, 'sync_interval_hours')
    if sync_hours_raw and not (sync_hours_raw.isdigit() and int(sync_hours_raw) >= 1):
        errors.append('Sync interval must be a positive whole number of hours.')
    return errors


def _find_duplicate_account(form, account_type: str, exclude_id=None):
    """Return an existing account of the same type whose identifying URL/login matches
    the submitted form, or None. Same-type only - an m3u account and an xtream account
    are never compared against each other, even if they'd turn out to be the same
    underlying subscription. This is advisory (see new_account, which flashes it, and
    save_account_api, which returns it as `warning` - neither blocks the save), so a
    same-type false negative is an acceptable trade for staying simple."""
    query = Account.query.filter(Account.account_type == account_type)
    if exclude_id is not None:
        query = query.filter(Account.id != exclude_id)
    if account_type == 'm3u':
        m3u_url = _field(form, 'm3u_url')
        if not m3u_url:
            return None
        return query.filter(Account.m3u_url == m3u_url).first()
    base_url = _field(form, 'base_url').rstrip('/')
    username = _field(form, 'username')
    if not base_url or not username:
        return None
    return query.filter(Account.base_url == base_url, Account.username == username).first()


def _duplicate_warning(dup, account_type: str) -> str:
    field = 'M3U URL' if account_type == 'm3u' else 'server and username'
    return f'Heads up: account "{dup.name}" already uses this {field}. Added anyway - check both are intentional.'


def _form_xmltv_fields(form, errors: list) -> dict | None:
    """The add-account form's optional XMLTV URL, as the fields of the url source it
    creates, or None when blank. Its validation errors are appended to `errors`."""
    url = _field(form, 'epg_url')
    if not url:
        return None
    problems, fields = clean_url_source_fields({'url': url}, _field(form, 'name'))
    errors.extend(problems)
    return fields


def _type_fields(form, account_type: str, existing=None) -> dict:
    """Return the account fields that differ by type, ready to set on the model.

    `Account.epg_url` is not among them and nothing writes it: an XMLTV URL is an EPG
    source (_add_form_xmltv_source, the Sources card), and m072 turned every stored one
    into a source (dev/changelog/1104)."""
    if account_type == 'm3u':
        return {
            'm3u_url': _field(form, 'm3u_url'),
            'base_url': '',
            'username': '',
            'password': '',
        }
    # xtream
    fields: dict = {
        'm3u_url': None,
        'base_url': _field(form, 'base_url').rstrip('/'),
        'username': _field(form, 'username'),
    }
    pw = form.get('password', '')
    if pw:
        fields['password'] = pw
    elif existing is not None:
        fields['password'] = existing.password  # keep existing password if blank
    else:
        fields['password'] = ''
    return fields
