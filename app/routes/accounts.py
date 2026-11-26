import threading
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from flask import (Blueprint, render_template, request, redirect, url_for, flash, current_app,
                   abort, jsonify)

from .. import channel_hiding, db, duplicate_streams, pip_channels
from ..accounts import (NORM_DISABLED, NORM_HLS, NORM_MODES, NORM_MPEGTS, NORM_MPEGTS_LIVE,
                        coerce_normalization_mode, global_normalization_mode,
                        norm_mode_example, norm_mode_label, normalize_url_with_mode,
                        resolve_normalization_mode, url_provider_stream_id,
                        finalize_sync_state,
                        manual_sync_restarts_schedule, next_sync_map,
                        report_source_removed, resolve_source_alerts,
                        start_source_refresh, sync_signature, update_source_expiry_alerts,
                        update_source_stale_alert)
from .. import account_stats_view
from ..fmt_utils import iso_or_none, plural
from ..account_links import (add_host, add_login, configured_seats, delete_hosts_for_account,
                             delete_logins_for_account, host_list, hosts_for_accounts,
                             is_refused_now, logins_for_accounts, move_login, next_stream,
                             playlist_rows_off, refusal_cooldown, remove_host, remove_login,
                             render_host, set_active_host, set_host_enabled, set_login_enabled,
                             unusable_reasons, update_login)
from ..account_stats import WINDOWS, today_local, windowed_stats
from ..channel_groups import clear_guide_listings_pins_on, report_orphaned_guide_groups
from ..channel_search import GROUP_ANY, OTHER_NEW, OTHER_REMOVED
from ..config import load_config, config_default
from ..alerts import mark_cleared
from ..database import (Account, AccountBlock, Alert, AccountLogin, AccountStatDay, Channel, Login,
                        AccountSyncLog, ChannelGroupMember, ChannelTest, EpgSource,
                        EpgSourceSubscription, SportsMatch,
                        EPG_FINDING_ADDS_NOTHING, EPG_FINDING_BETTER_SOURCE, EPG_FINDING_NAME_MATCHES,
                        EPG_FINDING_SHARED_ID, EPG_SOURCE_PROVIDER, EPG_SOURCE_URL, ACCOUNT_STATUS_SYNCING,
                        ACCOUNT_STATUS_UNSYNCED, SYNC_LOG_SUCCESS, SYNC_LOG_CANCELLED,
                        SYNC_LOG_IMPORTED, HOST_CHECK_ANSWERS, HOST_CHECK_RESOLVES,
                        HOST_PROBE_LOOKUP, detach_channel_references)
from ..db_utils import chunked, retry_on_locked
from ..epg_scorecard import (finding_text, followed_noun, games_text, name_games_text, recheck_soon,
                             stored_findings, stored_source_games)
from ..sports_match import name_games
from ..epg_sources import (MATCH_DISAGREE, MATCH_NEW, REFRESH_HOURS_CHOICES, comparison_title,
                           directory_row_summary,
                           case_sensitive_matching,
                           accept_name_matches, account_reach, account_sources_view, filled_counts_by_accounts, place_stretches,
                           stretch_channel_ids,
                           accounts_reach, inline_day, reach_words,
                           add_url_source, borrowable_sources, clean_url_source_fields,
                           delete_sources_for_account, delete_url_source,
                           ensure_provider_source, foreign_guided_channels, foreign_readers, has_directory,
                           forget_reader, ignore_shared_id, move_source, name_match_review,
                           propose_again, shared_ids,
                           refresh_busy_message, refreshing_source_names, reject_name_matches,
                           rejected_name_matches, reresolve_channels, subscriber_ids,
                           stop_using_source, subscribe_to_source, update_url_source,
                           use_source_again, NeedsOwnOrder, account_order_view,
                           follow_provider_again, move_source_to_provider, owner_label,
                           provider_account_ids,
                           set_account_guide, follow_trial, move_trial, preview_json,
                           stop_trial)
from ..logo_cache import delete_cached_logos
from ..storage_dirs import manual_screenshot_paths
from ..tz_utils import date_label, format_local, get_display_tz, is_24h, parse_form_time
from .. import account_activity, history_filter, host_checks, stream_check
from ..stream_check import delete_for_account as delete_stream_checks
from ..account_blocks import (NO_END, account_limits, add_account_block, add_login_block,
                              block_views,
                              describe, end_account_block, end_login_block,
                              login_block_views, rearm_waiting_starts,
                              recording_blocks, set_recording_blocks, until_label)
from ..ui_constants import DEFAULT_COLOR, PRESET_COLORS
from ..url_utils import mask_url_path
from .channels import _DELETE_CHUNK_SIZE
from .providers import account_provider, login_shares, providers_section, sibling_blocks

accounts_bp = Blueprint('accounts', __name__)

# The five sections of the account page, in their default order (DESIGN.md §17.3). The
# user's own order/hidden picks ride on the same server-side user-prefs store the group
# detail page uses, so a saved layout follows them across browsers - and a layout saved
# before a section existed shows it at its default place (util.js initSectionLayout).
ACCOUNT_SECTIONS = ['details', 'hosts', 'logins', 'content', 'sources', 'usage', 'history',
                    'activity']

# How many sync runs the Sync history section shows before its "All N syncs" control.
# Expanding loads the rest in place - there is no separate history page any more
# (dev/changelog/455; DESIGN.md §17.1 retired account_logs.html).
HISTORY_SHOWN = 10

# The per-account sync-interval choices. Blank means "follow sync.sync_interval_hours".
_SYNC_HOURS_CHOICES = [1, 2, 4, 6, 12, 24]

# How long a delete waits for a cancelled sync thread to exit before refusing. Bounded
# because this runs inside the request. Measured on a simulated 60,000-channel account
# (dev/changelog/1478): a cancel inside the channel loop or the guide import lands in under
# 0.2 s, and the stretches that cannot see one - the provider downloads and working through
# what they returned - run 8 to 50 s, which no in-request wait should cover. Five seconds is
# every checkpointed case with room to spare, and the refusal names the rest.
_SYNC_STOP_TIMEOUT_SECONDS = 5.0

# One row in the generic user-prefs store holds this page's section order and hidden set,
# so the layout follows the user across browsers (DESIGN.md §3.11).
_SECTIONS_PREF_KEY = 'account_detail_sections'

def _norm_options(cfg: dict) -> list[tuple[str, str, str]]:
    """('' = defer to the global default, labeled with the mode that default is now) + the
    four modes. The example strings are rendered under the dropdown so the mode names are
    unambiguous - see changelog/258 Spec §1."""
    current = norm_mode_label(global_normalization_mode(cfg))
    return [('', f'Use global default ({current})', '')] + [
        (value, label, example) for value, label, example in NORM_MODES
    ]


# What each mode does to a URL spelled some other way, for the Add Account page's table.
# The "after" column is the real normalizer's output, so the table cannot drift from what
# a sync does.
_NORM_TABLE_BEFORE = {
    NORM_MPEGTS: 'http://example.com/live/AAA/BBB/1.m3u8',
    NORM_MPEGTS_LIVE: 'http://example.com/AAA/BBB/1',
    NORM_HLS: 'http://example.com/AAA/BBB/1',
}
_NORM_TABLE = [
    (label, _NORM_TABLE_BEFORE[value], normalize_url_with_mode(_NORM_TABLE_BEFORE[value], value))
    for value, label, _example in NORM_MODES if value in _NORM_TABLE_BEFORE
]


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


def _sync_prompts(accounts, next_sync, cfg) -> dict:
    """{account_id: what the Sync now dialog needs} (account-actions.js::confirmAccountSync,
    dev/changelog/1134). One shape for both Accounts pages; `next_sync` is the batched
    next_sync_map() answer, so building a page's worth asks nothing per row."""
    from ..scheduler import account_sync_interval_hours
    restart = manual_sync_restarts_schedule(cfg)
    return {
        a.id: {
            'auto': bool(a.sync_enabled),
            'never_synced': a.last_sync_at is None,
            'next_at': iso_or_none(next_sync.get(a.id)),
            'interval_hours': account_sync_interval_hours(a, cfg),
            'restart_default': restart,
        }
        for a in accounts
    }


@accounts_bp.route('/accounts')
def accounts_list():
    """The Accounts list (DESIGN.md §17.1) and, below it, the account stats (§17.7). A row
    says which account this is, whether it is healthy, how big it is, and what it is right
    now; the stats section compares what the accounts did over a window.

    Everything the old fat card needed and this one does not (recording totals, the preset
    color swatches, the debug flag) is gone rather than passed and ignored: an unused
    template variable is a query the page pays for on every load."""
    from .. import sync_queue
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
    queue = sync_queue.status()
    # First, before anything else is loaded: it may commit a ledger catch-up, which expires
    # every row already in the session. It also loads the accounts, so the rows' second
    # line and the stats section below the list come from one call (dev/changelog/1029).
    cfg = load_config()
    stats = account_stats_view.section_context(window, cfg, current_app._get_current_object())
    accounts = stats['accounts']
    next_sync = next_sync_map(accounts)
    account_ids = [a.id for a in accounts]
    logs_by_account = _recent_logs_by_account(account_ids, limit=5)
    guide_counts_by_account = {aid: cur['guide_channels'] for aid, cur in stats['current'].items()}
    # The header's channel total is the sum of the column beneath it, deliberately - a
    # separately-queried total that disagreed with the visible rows would be worse than none.
    total_channels = sum(a.channel_count or 0 for a in accounts)
    total_hidden_channels = sum(a.hidden_channel_count or 0 for a in accounts)
    tz, now = get_display_tz(), datetime.utcnow()
    # The row's Programs cell has room for one date: the day most of the account's channels
    # reach. The tooltip carries the whole span.
    guide_reach = {aid: {'date': reach_words((r.most, r.most), now, tz, with_days=False),
                         'most': reach_words((r.most, r.most), now, tz),
                         'span': reach_words(r, now, tz)}
                   for aid, r in accounts_reach(account_ids, tz).items()}
    return render_template(
        'accounts.html',
        guide_reach=guide_reach,
        accounts=accounts,
        next_sync=next_sync,
        sync_prompts=_sync_prompts(accounts, next_sync, cfg),
        logs_by_account=logs_by_account,
        guide_counts=guide_counts_by_account,
        total_channels=total_channels,
        total_hidden_channels=total_hidden_channels,
        sync_sig=sync_sig,
        sync_queue=queue,
        stats=stats,
        blocks=block_views(account_ids),
        playlist_off=playlist_rows_off(account_ids),
        conn_limits=account_limits(account_ids, cfg),
        providers=providers_section(accounts),
        host_summaries=host_checks.account_summaries(account_ids),
        host_progress=host_checks.progress_view(),
        host_spacing=round(host_checks.spacing_seconds(cfg)),
    )


def _sync_log_json(log):
    """One sync run, in the one shape every consumer reads.

    The page's first paint and the expand-in-place fetch both go through this, so the
    history region has exactly one renderer fed by exactly one shape - a second spelling
    is how a first paint and a refresh end up disagreeing."""
    return {
        'id': log.id,
        'started_at': iso_or_none(log.started_at),
        'completed_at': iso_or_none(log.completed_at),
        'status': log.status,
        'channels': log.channels_synced or 0,
        'epg': log.epg_entries_synced or 0,
        'duration_seconds': ((log.completed_at - log.started_at).total_seconds()
                             if log.completed_at and log.started_at else None),
        'error': log.error_message or '',
    }


def _section_pref():
    """The stored {order, hidden} for the account page's sections, or None."""
    from .recordings import read_section_pref
    return read_section_pref(_SECTIONS_PREF_KEY)


def host_views(account_id):
    """The Hosts card's rows (DESIGN-account-providers.md §4.5, §6.3; dev/changelog/1674),
    a fixed number of queries. Each row carries `check`, the words for its newest verdict
    (host_checks.describe(), which names every status), or None when no full check has run -
    then `lookup` says what the last lookup found, if anything did. `queue` is 'checking',
    'waiting' or None, from the one host-check line."""
    rows = hosts_for_accounts([account_id]).get(account_id, [])
    latest = host_checks.latest_for_hosts(r.host for r in rows)
    line = host_checks.status()
    floor = datetime.utcnow() - host_checks.REPEAT_FLOOR
    out = []
    for row in rows:
        info = latest.get(row.host) or {}
        verdict = host_checks.effective(info)
        full, lookup, skipped = info.get('full'), info.get('lookup'), info.get('skipped')
        if lookup is not None:
            lookup_view = {'ok': lookup.status == HOST_CHECK_RESOLVES, 'at': lookup.checked_at,
                           'error': lookup.error}
        elif row.last_resolve_error:
            lookup_view = {'ok': False, 'at': None, 'error': row.last_resolve_error}
        elif row.last_resolved_at:
            lookup_view = {'ok': True, 'at': row.last_resolved_at, 'error': None}
        else:
            lookup_view = None
        if row.host == line['current']:
            queue, ahead = 'checking', 0
        elif row.host in line['line']:
            queue, ahead = 'waiting', line['line'].index(row.host) + (1 if line['current'] else 0)
        else:
            queue, ahead = None, 0
        out.append({
            'id': row.id,
            'host': row.host,
            'is_active': row.is_active,
            'activated_at': row.activated_at,
            'enabled': row.enabled,
            'from_playlist': row.from_playlist,
            'switched_at': row.switched_at,
            'check': dict(host_checks.describe(verdict), at=verdict.checked_at,
                          from_lookup=verdict.kind == HOST_PROBE_LOOKUP) if verdict else None,
            'answer_ms': (verdict.answer_ms if verdict is not None and verdict.status == HOST_CHECK_ANSWERS
                          and not verdict.in_use else None),
            'still_resolves_at': (lookup.checked_at if verdict is not None and verdict is full
                                  and lookup is not None and lookup.status == HOST_CHECK_RESOLVES
                                  and lookup.checked_at > full.checked_at else None),
            'lookup': lookup_view,
            # A "Doesn't resolve" full check a newer resolving lookup set aside
            # (dev/changelog/1731): untested again, but not "Not tested yet".
            'set_aside': host_checks.resolved_since(info),
            # The newest check skipped because a stream was using the host, when nothing
            # asked it since (dev/changelog/1730); the verdict above is still the last one.
            'skipped_at': (skipped.checked_at if skipped is not None and (
                full is None or skipped.checked_at > full.checked_at) else None),
            'answered': info.get('answered', 0),
            'checks': info.get('checks', 0),
            'queue': queue,
            'ahead': ahead,
            'recent_until': (full.checked_at + host_checks.REPEAT_FLOOR
                             if full is not None and full.checked_at > floor else None),
            'fastest': False,
        })
    # The fastest answer among the hosts that are on, marked only when there are two to compare.
    timed = [v for v in out if v['enabled'] and v['answer_ms'] is not None]
    if len(timed) >= 2:
        min(timed, key=lambda v: v['answer_ms'])['fastest'] = True
    return out

def unlisted_stream_host(account, hosts):
    """The host the account's streams are on when it is not one of the listed `hosts` (the
    card's rows), else None. With nothing listed it is the card's only row; with a list it
    shows only when the streams sit outside it, where a host swap cannot move them. Read
    from the sync's stamp, never by counting URLs here (dev/changelog/1176)."""
    host = account.stream_host
    if not host or any(h['host'] == host for h in hosts):
        return None
    return host


def own_login_view(account, logins):
    """The Logins card's row for the account's own login - its username and password on the
    account itself, which every sync uses (dev/changelog/1176). With nothing listed it is
    the login every capture uses, so it carries the account pool's seat count and holder
    line; once logins are listed, captures take seats on those and this row says syncs
    only. When a listed login has the same username it IS this login, so there is no
    second row: `same_as` names the listed row to mark instead. Carries no login id - the
    row has nothing the login endpoints could act on."""
    from .. import connection_limits as connlim
    if account.username:
        # Username and password together: a username may repeat on an account since the
        # playlist's expired login and a working one often share it (dev/changelog/1670).
        same = next((lg for lg in logins if lg['is_account_login']), None)
        if same is not None:
            return {'same_as': same['id']}
    if logins:
        if not account.username and any(lg['from_playlist'] for lg in logins):
            # An M3U account has no login of its own for a sync to use; once the playlist's
            # login is a row, a second "from the playlist" row would name it twice
            # (dev/changelog/1671).
            return None
        return {'same_as': None, 'username': account.username, 'syncs_only': True}
    held = connlim.holder_counts().get(account.id, 0)
    return {
        'same_as': None,
        'username': account.username,
        'syncs_only': False,
        'held': held,
        'holders': connlim.describe_holders(account.id) if held else '',
    }


def login_views(account_id, cfg):
    """The Logins card's rows (DESIGN-account-providers.md §5, §6.3): each login with its
    seats, what holds them right now, and whether the server refused it inside the cooldown
    (`refused`) or ever (`last_refused_at`). The password is never in the view. One query
    for the rows, one registry read for the holders."""
    from .. import connection_limits as connlim
    rows = logins_for_accounts([account_id]).get(account_id, [])
    if not rows:
        return []
    # A shared login is one pool: its count and its holder line cover every account that
    # holds it, and name the other account's holders (dev/changelog/1170).
    held = connlim.login_pool_counts([row.id for row in rows])
    shares = login_shares(account_id, [row.id for row in rows])
    names = None
    if shares or any(row.playlist_account_id not in (None, account_id) for row in rows):
        names = dict(db.session.query(Account.id, Account.name).all())
    cooldown = refusal_cooldown(cfg)
    now = datetime.utcnow()
    seats = configured_seats(rows, cfg)
    switches = {lid: (on, at) for lid, on, at in db.session.query(
        AccountLogin.login_id, AccountLogin.enabled, AccountLogin.switched_at)
        .filter(AccountLogin.account_id == account_id).all()}
    own_pair = db.session.query(Account.username, Account.password).filter(
        Account.id == account_id).first()
    blocks = login_block_views([row.id for row in rows])
    out = []
    for i, row in enumerate(rows):
        out.append({
            'id': row.id,
            'name': row.name,
            'username': row.username,
            'max_connections': seats[row.id],
            'enabled': bool(switches.get(row.id, (True, None))[0]),
            'switched_at': switches.get(row.id, (True, None))[1],
            'from_playlist': row.playlist_account_id == account_id,
            # Another account's playlist login, shared here through a provider: named by
            # whose playlist it is, never "the playlist" (this account's own).
            'playlist_of': (names.get(row.playlist_account_id, f'account {row.playlist_account_id}')
                            if row.playlist_account_id not in (None, account_id) else None),
            # The sync rewrites it and its seats are its account's Max connections, so it
            # has no Edit, and on its own account no Remove (account_links._is_sync_listed).
            'sync_listed': row.playlist_account_id is not None and row.max_connections is None,
            'is_account_login': own_pair is not None and bool(own_pair[0])
                                and (row.username, row.password) == tuple(own_pair),
            'held': held.get(row.id, 0),
            'holders': (connlim.describe_pool_holders(row.id, account_id=account_id, names=names)
                        if held.get(row.id) else ''),
            'shared_with': shares.get(row.id, []),
            # A block on the login itself (dev/changelog/1672): {'id', 'stop', 'stop_label'}.
            'blocked': blocks.get(row.id),
            'refused': is_refused_now(row.last_refused_at, now, cooldown),
            'last_refused_at': row.last_refused_at,
            'last_refused_detail': row.last_refused_detail,
            'first': i == 0,
            'last': i == len(rows) - 1,
        })
    return out


def _effective_settings(account, cfg, logins=()):
    """Each per-account setting, its effective value, and whether it was inherited.

    An inherited value is labelled `(global)` on the page (DESIGN.md §17.3), so "6h because
    this account says 6h" and "6h because Settings says 6h" are distinguishable at a glance -
    they behave differently the moment the global changes. While the account holds logins
    (`logins`, the card's rows) its connection limit is the sum of their seats and the page
    says `(logins: main, spare)` instead - Account.max_connections is not read for it."""
    global_hours = cfg.get('sync', {}).get('sync_interval_hours', config_default('sync.sync_interval_hours'))
    global_conn = cfg.get('accounts', {}).get('default_max_connections', 1)
    mode = resolve_normalization_mode(account, cfg)
    return {
        'interval_hours': account.sync_interval_hours or global_hours,
        'interval_inherited': account.sync_interval_hours is None,
        'max_connections': (sum(lg['max_connections'] for lg in logins if lg['enabled'])
                            if logins else account.max_connections or global_conn),
        'max_connections_inherited': account.max_connections is None and not logins,
        'max_connections_logins': ', '.join(lg['name'] for lg in logins if lg['enabled']),
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
    logs = [_sync_log_json(log) for log in rows]
    # The Activity card: syncs, alerts and blocks over the last ACTIVITY_DAYS, bucketed by the
    # history-filter chips and paged (app/account_activity.py). It renders ORM rows directly -
    # a different region from the history table, server-side, so it works with no JS.
    act_filter = history_filter.pick(request.args.get('hist'), account_activity.ACTIVITY_FILTERS)

    def _activity_url(key, shown=None):
        return url_for('accounts.account_detail', account_id=account.id,
                       hist=(key if key != history_filter.ALL else None), shown=shown,
                       w=request.args.get('w'))
    act_chips, act_entries = history_filter.apply(
        account_activity.entries(account), account_activity.bucket_of,
        account_activity.ACTIVITY_FILTERS, act_filter, _activity_url)
    page = account_activity.ACTIVITY_PAGE
    act_shown = max(page, min(request.args.get('shown', page, type=int) or page,
                              len(act_entries) or page))
    logins = login_views(account.id, cfg)
    total_syncs = _sync_log_totals(account_ids).get(account.id, 0)
    last_good = next((log for log in rows if log.status == SYNC_LOG_SUCCESS), None)
    # Queried rather than picked out of `rows`: the last ten runs can all be failures, and
    # a failed or cancelled run records no skip counts for the Content card to show.
    last_finished = (AccountSyncLog.query
                     .filter(AccountSyncLog.account_id == account.id,
                             AccountSyncLog.status.in_(SYNC_LOG_IMPORTED))
                     .order_by(AccountSyncLog.started_at.desc())
                     .first())
    tz, now = get_display_tz(), datetime.utcnow()
    sources = account_sources_view(account.id, _epg_source_schedule(), tz, now)
    # From the card's own rows, so the Content card's date and the sources it is made of
    # cannot disagree.
    span = account_reach({s['id']: s['active_here'] for s in sources},
                         {s['id']: s['reach_day'] for s in sources},
                         filled_counts_by_accounts([account.id]).get(account.id))
    # What the EPG check found this account could improve, as lines under each source
    # (dev/changelog/1718). Stored by the check rather than worked out here: the shared-id
    # half alone cost 0.5 s a page load on prod (dev/changelog/1716).
    found_by_source: dict[int, list] = {}
    names, h24 = {s['id']: s['name'] for s in sources}, is_24h()
    for f, at in stored_findings([account.id])[account.id]:
        line, tip = finding_text(f, names)
        found_by_source.setdefault(f.source_id, []).append({
            'line': line,
            'tip': f'{tip}\nFound {format_local(at, "monthday_time", tz=tz, h24=h24)}.',
            'warn': f.kind == EPG_FINDING_SHARED_ID, 'link': finding_link(f, account.id)})
    for s in sources:
        s['findings'] = found_by_source.get(s['id'], [])
    # Which followed sports games each source would match, stored by the same check
    # (dev/changelog/1726), and the ones read from channel names, which belong to no source.
    sports_games = None
    games = stored_source_games(account.id)
    if games:
        noun = followed_noun()
        listed_ids = set()
        by_source = {}
        for g, at in games:
            line, tip = games_text(g, lambda day: inline_day(date_label(day, now, tz)),
                                   format_local(at, 'monthday_time', tz=tz, h24=h24), noun)
            by_source[g.source_id] = {'line': line, 'tip': tip}
            listed_ids.update(g.event_ids)
        for s in sources:
            s['games'] = by_source.get(s['id'])
        named = name_games_text(name_games(account.id, now), listed_ids, noun)
        sports_games = {'line': named[0], 'tip': named[1]} if named else None
    return {
        'sports_names': sports_games,
        'cur': stats['current'][account.id],
        'group_any': GROUP_ANY,
        'all_time_note': account_stats_view.ALL_TIME_NOTE,
        'usage_all_time': usage_all_time,
        'logs': logs,
        'log_rows': rows,
        'act_entries': act_entries[:act_shown],
        'act_total': act_chips[0]['count'],
        'act_chips': act_chips,
        'act_more': max(0, len(act_entries) - act_shown),
        'act_more_url': _activity_url(act_filter, act_shown + page),
        'act_page': page,
        'act_days': account_activity.ACTIVITY_DAYS,
        'total_syncs': total_syncs,
        'has_more_syncs': total_syncs > len(logs),
        'last_good_sync': last_good,
        'last_finished_sync': last_finished,
        'logins': logins,
        'own_login': own_login_view(account, logins),
        'settings': _effective_settings(account, cfg, logins),
        # Which URL form the constructed stream URLs were built in. On the banner because a
        # constructed URL that does not play is nearly always the wrong form, so the form is
        # the first thing to check - it used to be named in the SYNC_STREAM_URLS_CONSTRUCTED
        # alert body, and moved here when that was retired (dev/changelog/928).
        'constructed_mode_label': norm_mode_label(resolve_normalization_mode(account, cfg)),
        # An account's own URL is secret in FULL - for a path-token provider the path IS
        # the credential, and no heuristic can tell (DESIGN-secrets.md §4.2, DESIGN.md §17.4).
        'endpoint': mask_url_path(account.base_url if (account.account_type or 'm3u') == 'xtream'
                                  else account.m3u_url),
        'stream_host_differs': _stream_host_differs(account),
        'epg_sources': sources,
        'epg_order': account_order_view(account),
        'guide_reach': reach_words(span, now, tz),
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


def finding_link(f, account_id: int) -> tuple[str, str] | None:
    """(label, url) of the page where an account's EPG finding is acted on, or None where a
    line says it in words (dev/changelog/1718). Name matches open on that account's
    channels only, the ones the finding counts."""
    if f.kind == EPG_FINDING_NAME_MATCHES:
        return 'Review name matches', url_for('accounts.epg_source_review',
                                              source_id=f.source_id, unguided='1',
                                              account=account_id)
    if f.kind == EPG_FINDING_SHARED_ID:
        return 'Shared ids', url_for('accounts.epg_source_review', source_id=f.source_id,
                                     view='shared')
    if f.kind in (EPG_FINDING_BETTER_SOURCE, EPG_FINDING_ADDS_NOTHING):
        return None
    raise ValueError(f'unknown EPG finding kind {f.kind!r}')


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
    # Read before the rows it vouches for, as accounts_list does: a sync that moves in
    # between costs one extra refresh, where a signature read after would vouch for a state
    # the page never showed. Scalars only, so the fold below expires nothing of it.
    sync_sig = sync_signature()
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
    hosts = host_views(account.id)
    next_sync = next_sync_map([account])
    # The playlist's own rows, read off the cards' views: the notice, the header badge and the
    # rows themselves cannot disagree about which one is off (dev/changelog/1671).
    playlist_host = next((h for h in hosts if h['from_playlist']), None)
    playlist_login = next((lg for lg in payload['logins'] if lg['from_playlist']), None)
    return render_template(
        'account_detail.html',
        account=account,
        next_sync_at=next_sync[account.id],
        sync_prompt=_sync_prompts([account], next_sync, cfg)[account.id],
        account_type=(account.account_type or 'm3u'),
        preset_colors=PRESET_COLORS,
        default_color=DEFAULT_COLOR,
        norm_options=_norm_options(cfg),
        sync_hours_choices=_SYNC_HOURS_CHOICES,
        sections=ACCOUNT_SECTIONS,
        section_pref=_section_pref(),
        # Effective for THIS account - global flag OR its own override - so the kebab
        # menu and the mobile action sheet both light up correctly either way.
        xtream_debug=_xtream_debug_enabled(account),
        stats=stats,
        hosts=hosts,
        host_progress=host_checks.progress_view([h['host'] for h in hosts]),
        host_spacing=round(host_checks.spacing_seconds(cfg)),
        host_not_answering=host_checks.not_answering_accounts([account.id]).get(account.id),
        unlisted_host=unlisted_stream_host(account, hosts),
        playlist_host_off=playlist_host if playlist_host and not playlist_host['enabled'] else None,
        playlist_login_off=(playlist_login if playlist_login and not playlist_login['enabled']
                            else None),
        unusable=unusable_reasons([account.id]).get(account.id),
        next_stream=next_stream(account, cfg),
        blocks=block_views([account.id]).get(account.id, []),
        sibling_blocks=sibling_blocks(account.id),
        provider=account_provider(account),
        sync_sig=sync_sig,
        stream_check=stream_check.view(stream_check.latest(account.id)),
        **payload,
    )


@accounts_bp.route('/accounts/new', methods=['GET', 'POST'])
def new_account():
    cfg = load_config()
    global_sync_hours = cfg.get('sync', {}).get('sync_interval_hours', config_default('sync.sync_interval_hours'))
    global_max_connections = cfg.get('accounts', {}).get('default_max_connections', 1)

    if request.method == 'POST':
        errors = _validate_account_form(request.form, new=True)
        xmltv = _form_xmltv_fields(request.form, errors)
        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template(
                'account_form.html',
                account=None,
                form=request.form,
                preset_colors=PRESET_COLORS,
                default_color=DEFAULT_COLOR,
                norm_options=_norm_options(cfg),
                norm_table=_NORM_TABLE,
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
                color=form.get('color', DEFAULT_COLOR),
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
            # An M3U account's EPG URL is its own guide, the one that fills its provider's
            # "Provider's EPG" slot (DESIGN-epg-sources.md §15.3).
            source_id = (add_url_source(account, xmltv, account_guide=account_type != 'xtream').id
                         if xmltv else None)
            if _checkbox(form.get('stream_check')):
                stream_check.owe_after_first_sync(account.id)
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
        flash(_added_message(account_id, account_name), 'success')
        return redirect(url_for('accounts.accounts_list'))

    return render_template(
        'account_form.html',
        account=None,
        form={'sync_enabled': True},
        preset_colors=PRESET_COLORS,
        default_color=DEFAULT_COLOR,
        norm_options=_norm_options(cfg),
        norm_table=_NORM_TABLE,
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

def _stream_host_differs(account) -> bool:
    """Whether the provider serves this Xtream account's streams from another host than the
    one its channel list comes from - normal, and said so on the account page, because two
    hosts side by side otherwise read as a fault (dev/changelog/1272). Hostnames only: the
    page shows both URLs in full for anyone who needs the scheme and port."""
    if (account.account_type or 'm3u') != 'xtream' or not account.provider_stream_origin:
        return False
    api = urlsplit(account.base_url or '').hostname
    streams = urlsplit(account.provider_stream_origin).hostname
    return bool(api and streams and api.lower() != streams.lower())


def _added_message(account_id: int, name: str) -> str:
    """The toast after Add Account: when the first sync actually starts, as the scheduler
    registered it, and where Sync now is on the page the toast lands on - the row's menu
    (dev/changelog/1272)."""
    from .recordings import time_until_filter

    # A column query, not the identity map: schedule_account_sync() wrote next_sync_at
    # from its own app context, so the Account loaded by the insert above is stale.
    sync_enabled, next_at = (db.session.query(Account.sync_enabled, Account.next_sync_at)
                             .filter(Account.id == account_id).one())
    added = f'Account "{name}" added.'
    if not sync_enabled:
        return (f'{added} Automatic sync is off, so nothing is fetched until you choose '
                'Sync now from its ⋯ menu.')
    # A first sync is registered a minute out (scheduler.schedule_account_sync), and the page
    # shows it running at the next status poll after that: "under a minute" was a hair true
    # and read as broken at 70 seconds (dev/changelog/1291).
    now = datetime.utcnow()
    if not next_at or next_at <= now:
        when = 'starts shortly'
    elif (next_at - now).total_seconds() <= 90:
        when = 'starts in about a minute'
    else:
        when = f'starts {time_until_filter(next_at)}'
    return (f'{added} Its first sync {when} and fetches its channels and guide. '
            'To start it right away, choose Sync now from its ⋯ menu.')


def _start_sync(account, *, force=False, force_epg_resync=False, restart_schedule=False):
    """Start a manual sync. Returns (started, message, conflicts).

    `conflicts` is the list of reasons a concurrency clash blocked the start, which the
    caller surfaces individually with a "Sync anyway" affordance - it is a warning the user
    may override, not a refusal (DESIGN-concurrency.md 5.4). It is returned as the list
    rather than a bool because the user is being asked to decide, and "something else is
    running" is not enough to decide on. Empty list means no clash: either the sync started,
    or it failed for a reason force cannot fix. `force_epg_resync` is a separate,
    independently-meaning flag: it bypasses the EPG collapse guard for this one run
    (DESIGN-sync-resilience.md §4), and is enforced here rather than in the UI.
    `restart_schedule` lets a successful run stand in for the next scheduled sync; it means
    nothing for an account with automatic sync off, which has no schedule to move."""
    from ..accounts import run_manual_sync, sync_conflicts

    if account.status == ACCOUNT_STATUS_SYNCING:
        return False, f'"{account.name}" is already syncing.', []

    if not force:
        conflicts = sync_conflicts(account.id)
        if conflicts:
            return (False,
                    f'Sync not started for "{account.name}": ' + ' '.join(conflicts),
                    conflicts)

    restart_schedule = restart_schedule and account.sync_enabled
    threading.Thread(
        target=run_manual_sync,
        args=(current_app._get_current_object(), account.id),
        kwargs={'force_epg_resync': force_epg_resync, 'restart_schedule': restart_schedule},
        daemon=True,
        name=f'account-sync-{account.id}',
    ).start()
    message = f'Sync started for "{account.name}".'
    if restart_schedule:
        message += ' If it succeeds, it replaces the next scheduled sync.'
    return True, message + ' Refresh in a moment to see results.', []


def _cancel_sync(account):
    """Cancel a running sync, or reset one left stranded by a restart. Returns the message."""
    from ..accounts import cancel_sync

    account_id, name = account.id, account.name
    if cancel_sync(account_id) != 'reset':
        return f'Cancel signal sent for "{name}". Refresh in a moment.'

    @retry_on_locked()
    def _reset():
        finalize_sync_state(
            account_id, ACCOUNT_STATUS_UNSYNCED, SYNC_LOG_CANCELLED,
            account_message='Sync reset (was stuck after service restart)',
            log_message='Reset after service restart',
        )
        db.session.commit()

    _reset()
    return f'Sync state for "{name}" has been reset.'


def _sync_still_running_message(account_id):
    """Why a delete was refused, naming the phase the sync is in when it reports one.

    The phase is what makes the refusal actionable: "still syncing" alone doesn't tell the
    user whether they are waiting seconds or minutes. The reason it gives is the measured
    one (dev/changelog/1478): a sync between checkpoints stops at once, so a thread still
    alive after the wait is downloading from the provider or working through what it got."""
    from ..accounts import get_sync_progress

    account = db.session.get(Account, account_id)
    label = f'"{account.name}"' if account else 'This account'
    phase = (get_sync_progress(account_id) or {}).get('phase')
    where = f' ({phase} phase)' if phase else ''
    return (f'{label} is still syncing{where} and has been told to stop. It stops within a '
            'moment once it is working through channels or listings, but not while it is '
            'downloading from the provider or sorting out what it downloaded - try the '
            'delete again in a minute.')


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
    # Other accounts' channels whose guide one of these sources continues: a source behind
    # it may reach past the end once it is gone (epg_sources.place_stretches()).
    mine = {cid for (cid,) in db.session.query(Channel.id).filter(
        Channel.account_id == account_id)} if owned_source_ids else set()
    filled_elsewhere = sorted({cid for sid in owned_source_ids
                               for cid in stretch_channel_ids(sid)} - mine)
    read_elsewhere = [sid for (sid,) in db.session.query(EpgSourceSubscription.source_id)
                      .join(EpgSource, EpgSource.id == EpgSourceSubscription.source_id)
                      .filter(EpgSourceSubscription.account_id == account_id,
                              db.or_(EpgSource.owner_account_id.is_(None),
                                     EpgSource.owner_account_id != account_id))]

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

    # Screenshots are files, not rows, for the same reason: the cascade takes every
    # ChannelTest and the channel rows that say a hand capture exists, and leaves the images
    # on disk. Read before the delete, unlinked after it has committed (dev/changelog/1160).
    cfg = load_config()
    channel_ids = [cid for (cid,) in db.session.query(Channel.id).filter(
        Channel.account_id == account_id).all()]
    screenshot_paths = []
    for chunk in chunked(channel_ids, _DELETE_CHUNK_SIZE):
        screenshot_paths.extend(
            p for (p,) in db.session.query(ChannelTest.screenshot_path)
            .filter(ChannelTest.channel_id.in_(chunk))
            .filter(ChannelTest.screenshot_path.isnot(None)).all())
        screenshot_paths.extend(manual_screenshot_paths(cfg, chunk))

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

    pip_rows = set()

    @retry_on_locked()
    def _delete():
        pip_rows.clear()    # a retried attempt names only what it deleted
        account = db.session.get(Account, account_id)
        if account is None:
            return None
        name = account.name
        # Not in the ORM cascade: the ledger has no relationship on Account. Account ids are
        # never re-issued (migration 107), so a row left behind would credit nobody - it would
        # sit in the table for good. The fold refuses a gone account in the statement that
        # writes, so it cannot put one back after this commit (dev/changelog/1373).
        AccountStatDay.query.filter_by(account_id=account_id).delete(synchronize_session=False)
        # Nor its blocks, for the same reason.
        AccountBlock.query.filter_by(account_id=account_id).delete(synchronize_session=False)
        # Nor its stream checks, whose retry jobs go with them.
        delete_stream_checks(account_id)
        # Nor its host list (app/account_links.py is its one writer), nor its login list and
        # the logins no other account holds.
        delete_hosts_for_account(account_id)
        delete_logins_for_account(account_id)
        # Nor are its EPG sources, which may hold hundreds of thousands of rows across two
        # tables and are bulk-deleted rather than cascaded (DESIGN-epg-sources.md §9.5).
        delete_sources_for_account(account_id)
        # Its standing alerts describe an account that no longer exists; account ids are
        # never re-issued, so every `account:<id>:` source is this account's (dev/changelog/1732).
        mark_cleared(Alert.query.filter(Alert.source.like(f'account:{account_id}:%'),
                                        Alert.dismissed_at.is_(None)).all())
        # A group pinned to one of these channels goes back to automatic, and says why.
        clear_guide_listings_pins_on(channel_ids)
        # Not cascaded either, and channel ids are re-issued (DESIGN-sports-data.md 9): a
        # match goes, and a finished recording, its segments and a group's events lose the id.
        for chunk in chunked(channel_ids, _DELETE_CHUNK_SIZE):
            SportsMatch.query.filter(SportsMatch.channel_id.in_(chunk)).delete(synchronize_session=False)
            detach_channel_references(chunk, pip_rows=pip_rows)
        # hidden-recompute-ok: the cascade takes memberships only of channels it deletes
        # too, so no surviving channel loses a protection.
        db.session.delete(account)
        # A deleted channel may have been the copy its cluster kept; without this the
        # copies on other accounts stay folded behind a row that no longer exists.
        db.session.flush()
        duplicate_streams.recompute(cfg)
        db.session.commit()
        return name

    name = _delete()
    if channel_ids:
        # Its channels leave every rule that matched them (dev/changelog/1291).
        channel_hiding.refresh_rule_stats_and_commit()
    for sid in owned_source_ids:
        resolve_source_alerts(sid)
    outcome = (reresolve_channels(list(guided_elsewhere), _case_sensitive(),
                                  previous=guided_elsewhere, source_names=source_names)
               if guided_elsewhere else {})
    if filled_elsewhere:
        place_stretches(filled_elsewhere, _case_sensitive())
    for sid in owned_source_ids:
        report_source_removed(sid, source_names[sid],
                              f'with its account "{name}"' if name else 'with its account',
                              readers, guided_elsewhere, outcome)
    if read_elsewhere:
        forget_reader(read_elsewhere)
    if readers:
        # Other accounts lost a source they read; their findings are re-found.
        recheck_soon(f'account "{name}" deleted with sources other accounts read')
    if cached_paths:
        delete_cached_logos(cached_paths)
    if name is not None and screenshot_paths:
        from ..recorder import delete_files
        delete_files(screenshot_paths)
    report_orphaned_guide_groups(
        touched_group_ids,
        cause=f'The channels it could record from belonged to the deleted account "{name}".'
        if name else 'The channels it could record from belonged to a deleted account.')
    pip_channels.refresh_detached(pip_rows, 'channel')
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

    if account.status == ACCOUNT_STATUS_SYNCING:
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
                         for v, label, example in _norm_options(cfg)],
        'sync_hours_choices': _SYNC_HOURS_CHOICES,
        'preset_colors': [{'hex': hexval, 'label': label} for hexval, label in PRESET_COLORS],
        'default_color': DEFAULT_COLOR,
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
def _renormalize_chunk(chunk_ids, mode, listed_hosts=frozenset(), active_host=None):
    """Recompute stream_url/url_normalizable/provider_stream_id for one chunk of channels
    from their stored raw_stream_url, under `mode`, with the account's active host rendered
    onto every URL whose raw host is listed (`listed_hosts`/`active_host`, the caller's one
    `host_list()` read) - exactly what the sync writes, so a re-normalize can never put the
    account back on a host it rolled off (dev/changelog/1243). Deterministic and idempotent
    (never inserts a row), so a lock-retry re-running the whole chunk is always safe -
    unlike an INSERT, there is no duplicate-row risk from CLAUDE.md's "one commit per
    decorated closure" rule."""
    rows = Channel.query.filter(Channel.id.in_(chunk_ids)).all()
    mappings = []
    stamped_at = datetime.utcnow()
    for ch in rows:
        if not ch.raw_stream_url:
            continue
        new_url = render_host(normalize_url_with_mode(ch.raw_stream_url, mode),
                              listed_hosts, active_host)
        # One parse for both, as in _upsert_channels, so the pair can never disagree.
        provider_sid = url_provider_stream_id(ch.raw_stream_url)
        normalizable = provider_sid is not None
        if (new_url != ch.stream_url or normalizable != ch.url_normalizable
                or provider_sid != ch.provider_stream_id):
            row = {'id': ch.id, 'stream_url': new_url, 'url_normalizable': normalizable,
                   'provider_stream_id': provider_sid}
            if new_url != ch.stream_url:
                # stream_url is one of the four columns ch_fts indexes, so rewriting it
                # genuinely moves the channels search index's source and has to stamp the
                # watermark (app/database.py::Channel.search_text_updated_at), as the two
                # other writers of stream_url do (_upsert_channels and the host roll's
                # account_links.rerender_stream_urls); url_normalizable and
                # provider_stream_id are not indexed and must not stamp on their own.
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
    listed_hosts, active_host = host_list(account_id)
    changed = 0
    for chunk in chunked(ids, _DELETE_CHUNK_SIZE):
        changed += _renormalize_chunk(chunk, mode, listed_hosts, active_host)
    if changed:
        duplicate_streams.recompute_and_commit()

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


def _block_stop(data, now, what):
    """(the stop a block's body asks for - None for `until_unblocked` - , None) or (None, an
    error response). The account block and the login block take one body shape, so their
    dialogs offer the same choices: `minutes`; `until` beside `until_utc`; or
    `until_unblocked: true`."""
    if data.get('until_unblocked') is True:
        return None, None
    try:
        if data.get('until'):
            stop = parse_form_time(str(data['until']), str(data.get('until_utc') or ''))
        else:
            stop = now + timedelta(minutes=int(data.get('minutes')))
    except (TypeError, ValueError, OverflowError):
        return None, (jsonify({'error': f'Say how long to block {what} for.'}), 400)
    if stop <= now:
        return None, (jsonify({'error': 'That time has already passed.'}), 400)
    return stop, None


@accounts_bp.route('/api/accounts/<int:account_id>/blocks', methods=['POST'])
def block_account_api(account_id):
    """Block the account from now (app/account_blocks.py). Body: `minutes`; or `until` as a
    datetime-local value in the display timezone beside `until_utc`, the instant the field
    stands for (util.js::wallTimeField - a wall time a DST change repeats cannot say which
    pass it means, dev/changelog/1477); or `until_unblocked: true` for a block with no end,
    which holds until the user unblocks it (dev/changelog/1667). Optional `slots` (omitted
    or null = every slot). No upper bound on a timed block: one that may have no end at all
    makes a cap on the timed ones meaningless."""
    account, err = _json_account(account_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    now = datetime.utcnow()
    stop, err = _block_stop(data, now, 'the account')
    if err:
        return err
    slots = data.get('slots')
    if slots in ('', None):
        slots = None
    else:
        try:
            slots = int(slots)
        except (TypeError, ValueError):
            return jsonify({'error': 'Slots must be a whole number.'}), 400
        if slots < 1:
            return jsonify({'error': 'Slots must be at least 1.'}), 400
    add_account_block(account_id, stop, start=now, slots=slots)
    return jsonify({'success': True,
                    'message': f'{describe(account.name, stop or NO_END, capital=True)}.'})


@accounts_bp.route('/api/accounts/<int:account_id>/blocks/<int:block_id>', methods=['DELETE'])
def unblock_account_api(account_id, block_id):
    """End one block. A block set on a recording is taken off that recording, which says so
    on its own timeline; any start that was waiting on the block is re-armed at once."""
    account, err = _json_account(account_id)
    if err:
        return err
    block = db.session.get(AccountBlock, block_id)
    if block is None or block.account_id != account_id:
        return jsonify({'error': 'Block not found.'}), 404
    if block.recording_id is not None:
        wanted = recording_blocks(block.recording_id)
        wanted.pop(account_id, None)
        set_recording_blocks(block.recording_id, wanted)
    else:
        end_account_block(block_id)
    rearm_waiting_starts()
    return jsonify({'success': True, 'message': f'Account "{account.name}" unblocked.'})


@accounts_bp.route('/api/accounts/<int:account_id>/hosts', methods=['POST'])
def add_host_api(account_id):
    """Add a host to the account's list (app/account_links.py). Body: `host`, a bare
    `name[:port]` or a pasted URL, which is reduced to its host. The first add also seeds
    the host the account's URLs carry today as the active one, and the response says so."""
    account, err = _json_account(account_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    before = hosts_for_accounts([account_id]).get(account_id, [])
    try:
        row = add_host(account_id, str(data.get('host') or ''))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    after = hosts_for_accounts([account_id]).get(account_id, [])
    seeded = [r.host for r in after if r.id != row.id and r.id not in {b.id for b in before}]
    message = f'{row.host} added to {account.name}\'s host list.'
    if seeded:
        message += (f' {seeded[0]} is the host this account\'s stream URLs use now, so it was '
                    f'listed first and is the active one.')
    elif row.is_active:
        message += ' It is the active host.'
    return jsonify({'success': True, 'message': message, 'host_id': row.id})


@accounts_bp.route('/api/accounts/<int:account_id>/hosts/<int:host_id>', methods=['DELETE'])
def remove_host_api(account_id, host_id):
    """Take a host off the list. The active host is refused while another is listed."""
    account, err = _json_account(account_id)
    if err:
        return err
    try:
        name = remove_host(account_id, host_id)
    except LookupError as exc:
        return jsonify({'error': str(exc)}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    return jsonify({'success': True, 'message': f'{name} removed from {account.name}\'s host list.'})


@accounts_bp.route('/api/accounts/<int:account_id>/hosts/<int:host_id>/activate', methods=['POST'])
def activate_host_api(account_id, host_id):
    """The user's own switch: every stream URL on the account moves to this host now."""
    account, err = _json_account(account_id)
    if err:
        return err
    try:
        summary = set_active_host(account_id, host_id)
    except LookupError as exc:
        return jsonify({'error': str(exc)}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    n = summary['channels']
    message = (f'{account.name} now uses {summary["host"]}: {n:,} stream {plural(n, "URL")} '
               f'rewritten')
    if summary['account_urls']:
        message += ', and the account\'s own URL'
    message += '.'
    return jsonify({'success': True, 'message': message, **summary})


def _switch_value(data):
    """The `enabled` field of a switch's body, which must be a real JSON boolean: a string
    like "false" is truthy, and reading it as on would turn on a row the user turned off."""
    value = data.get('enabled')
    return value if isinstance(value, bool) else None


@accounts_bp.route('/api/accounts/<int:account_id>/hosts/<int:host_id>/enabled', methods=['POST'])
def host_enabled_api(account_id, host_id):
    """The Use switch on one host (dev/changelog/1670). Body: `enabled`, true or false.
    Turning the active host off hands the role to the next host that is on."""
    account, err = _json_account(account_id)
    if err:
        return err
    enabled = _switch_value(request.get_json(silent=True) or {})
    if enabled is None:
        return jsonify({'error': 'Send "enabled" as true or false.'}), 400
    try:
        out = set_host_enabled(account_id, host_id, enabled)
    except LookupError as exc:
        return jsonify({'error': str(exc)}), 404
    if enabled:
        message = f'{out["host"]} turned on.'
    else:
        message = (f'{out["host"]} turned off. Streams already running keep going; new ones '
                   f'won\'t use it.')
    if out['moved']:
        message += f' {account.name} now uses {out["active"]}.'
    elif not enabled and out['active'] == out['host']:
        message += f' No other host on {account.name} is turned on, so nothing on it can stream.'
    return jsonify({'success': True, 'message': message, **out})


@accounts_bp.route('/api/accounts/<int:account_id>/hosts/<int:host_id>/check', methods=['POST'])
def check_host_api(account_id, host_id):
    """Check now on one host: a full check, put in the one host-check line (app/host_checks.py).
    A host given a full check in the last 10 minutes is left out, and the message says when
    it can be checked again."""
    from ..database import AccountHost
    account, err = _json_account(account_id)
    if err:
        return err
    row = db.session.get(AccountHost, host_id)
    if row is None or row.account_id != account_id:
        return jsonify({'error': 'Host not found.'}), 404
    result = host_checks.request(current_app._get_current_object(), [row.host])
    return jsonify({'success': True, 'queued': len(result['added']),
                    'message': host_checks.request_message(result, account.name)})


@accounts_bp.route('/api/accounts/<int:account_id>/hosts/check', methods=['POST'])
def check_account_hosts_api(account_id):
    """Check all on one account's Hosts card: every host on it, turned off or not."""
    account, err = _json_account(account_id)
    if err:
        return err
    hosts = [r.host for r in hosts_for_accounts([account_id]).get(account_id, [])]
    result = host_checks.request(current_app._get_current_object(), hosts)
    return jsonify({'success': True, 'queued': len(result['added']),
                    'message': host_checks.request_message(result, account.name)})


@accounts_bp.route('/api/accounts/hosts/check', methods=['POST'])
def check_all_hosts_api():
    """Check all hosts on the Accounts list: every host name on every account, once each."""
    from ..database import AccountHost
    hosts = [h for (h,) in db.session.query(AccountHost.host).order_by(
        AccountHost.account_id, AccountHost.position, AccountHost.id).all()]
    result = host_checks.request(current_app._get_current_object(), hosts)
    return jsonify({'success': True, 'queued': len(result['added']),
                    'message': host_checks.request_message(result, 'any account')})


@accounts_bp.route('/api/accounts/hosts/check/stop', methods=['POST'])
def stop_host_checks_api():
    """Stop checking: empty the line. A check already running finishes."""
    n = host_checks.stop()
    return jsonify({'success': True, 'stopped': n,
                    'message': (f'Stopped. {n} {plural(n, "host")} left unchecked.'
                                if n else 'Nothing was waiting to be checked.')})


def _login_seat_prefill(account):
    """What the add-login dialog pre-fills seats with: the Xtream-reported cap when one is
    known, else the account's effective limit (DESIGN-account-providers.md §3.2)."""
    if account.provider_max_connections:
        return int(account.provider_max_connections)
    return int(account.max_connections
               or load_config().get('accounts', {}).get('default_max_connections', 1))


@accounts_bp.route('/api/accounts/<int:account_id>/logins', methods=['GET'])
def logins_api(account_id):
    """The add/edit dialogs' values: every login without its password (never served back,
    as the account's own password is not), and the seat count a new one pre-fills with."""
    account, err = _json_account(account_id)
    if err:
        return err
    return jsonify({
        'success': True,
        'logins': [{'id': lg['id'], 'name': lg['name'], 'username': lg['username'],
                    'max_connections': lg['max_connections']}
                   for lg in login_views(account_id, load_config())],
        'seat_prefill': _login_seat_prefill(account),
    })


def _login_fields(data):
    return (str(data.get('name') or ''), str(data.get('username') or ''),
            str(data.get('password') or ''), data.get('max_connections'))


@accounts_bp.route('/api/accounts/<int:account_id>/logins', methods=['POST'])
def add_login_api(account_id):
    """Add a login to the account's list (app/account_links.py). Body: `name`, `username`,
    `password`, `max_connections`."""
    account, err = _json_account(account_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        row = add_login(account_id, *_login_fields(data))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'success': True, 'login_id': row.id,
                    'message': (f'Login "{row.name}" added to {account.name} with '
                                f'{row.max_connections} {plural(row.max_connections, "seat")}.')})


@accounts_bp.route('/api/accounts/<int:account_id>/logins/<int:login_id>', methods=['POST'])
def update_login_api(account_id, login_id):
    """Edit a login. A blank `password` keeps the stored one."""
    account, err = _json_account(account_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        row = update_login(account_id, login_id, *_login_fields(data))
    except LookupError as exc:
        return jsonify({'error': str(exc)}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'success': True, 'message': f'Login "{row.name}" saved.'})


@accounts_bp.route('/api/accounts/<int:account_id>/logins/<int:login_id>', methods=['DELETE'])
def remove_login_api(account_id, login_id):
    """Take a login off the list. A capture seated on it keeps its seat until it ends."""
    account, err = _json_account(account_id)
    if err:
        return err
    try:
        name = remove_login(account_id, login_id)
    except LookupError as exc:
        return jsonify({'error': str(exc)}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    return jsonify({'success': True,
                    'message': f'Login "{name}" removed from {account.name}\'s list.'})


@accounts_bp.route('/api/accounts/<int:account_id>/logins/<int:login_id>/enabled', methods=['POST'])
def login_enabled_api(account_id, login_id):
    """The Use switch on one login, for this account only (dev/changelog/1670). Body:
    `enabled`, true or false."""
    account, err = _json_account(account_id)
    if err:
        return err
    enabled = _switch_value(request.get_json(silent=True) or {})
    if enabled is None:
        return jsonify({'error': 'Send "enabled" as true or false.'}), 400
    try:
        out = set_login_enabled(account_id, login_id, enabled)
    except LookupError as exc:
        return jsonify({'error': str(exc)}), 404
    if enabled:
        message = f'Login "{out["login"]}" turned on for {account.name}.'
    else:
        message = (f'Login "{out["login"]}" turned off for {account.name}. Streams already '
                   f'running keep going; new ones won\'t use it.')
    return jsonify({'success': True, 'message': message, **out})


def _held_login(account_id, login_id):
    """(the Login row, None) when the account holds it, else (None, a 404 response)."""
    held = db.session.query(Login).join(AccountLogin, AccountLogin.login_id == Login.id).filter(
        AccountLogin.account_id == account_id, Login.id == login_id).first()
    if held is None:
        return None, (jsonify({'error': 'Login not found.'}), 404)
    return held, None


@accounts_bp.route('/api/accounts/<int:account_id>/logins/<int:login_id>/block', methods=['POST'])
def block_login_api(account_id, login_id):
    """Block one login from now (dev/changelog/1672): no new stream takes a seat on it, on any
    account holding it; streams already running on it keep going. The body is the account
    block's (`minutes`, `until` + `until_utc`, or `until_unblocked: true`), with no `slots`:
    the block takes the whole login."""
    account, err = _json_account(account_id)
    if err:
        return err
    login, err = _held_login(account_id, login_id)
    if err:
        return err
    stop, err = _block_stop(request.get_json(silent=True) or {}, datetime.utcnow(), 'the login')
    if err:
        return err
    add_login_block(login_id, stop)
    others = login_shares(account_id, [login_id]).get(login_id, [])
    message = f'Login "{login.name}" blocked until {until_label(stop or NO_END)}.'
    if others:
        message += f' That covers {", ".join(others)} too.'
    return jsonify({'success': True, 'message': message})


@accounts_bp.route('/api/accounts/<int:account_id>/logins/<int:login_id>/block', methods=['DELETE'])
def unblock_login_api(account_id, login_id):
    """End the block on one login; any start that was waiting on it is re-armed at once."""
    account, err = _json_account(account_id)
    if err:
        return err
    login, err = _held_login(account_id, login_id)
    if err:
        return err
    if not end_login_block(login_id):
        return jsonify({'success': True, 'message': f'Login "{login.name}" was not blocked.'})
    rearm_waiting_starts()
    return jsonify({'success': True, 'message': f'Login "{login.name}" unblocked.'})


@accounts_bp.route('/api/accounts/<int:account_id>/logins/<int:login_id>/move', methods=['POST'])
def move_login_api(account_id, login_id):
    """Move a login one step up or down the list - the order seats are taken in. Body:
    `direction`, `up` or `down`."""
    account, err = _json_account(account_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        order = move_login(account_id, login_id, str(data.get('direction') or ''))
    except LookupError as exc:
        return jsonify({'error': str(exc)}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'success': True, 'order': order})


@accounts_bp.route('/api/accounts/<int:account_id>/sync', methods=['POST'])
def sync_account_api(account_id):
    account, err = _json_account(account_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    # The Sync now dialog always sends its switch. A caller that does not ask (the Force EPG
    # resync confirm) gets the configured default, so the setting means the same thing on
    # every path that starts a manual sync.
    restart_schedule = (_checkbox(data['restart_schedule']) if 'restart_schedule' in data
                        else manual_sync_restarts_schedule(load_config()))
    started, message, conflicts = _start_sync(
        account,
        force=_checkbox(data.get('force')),
        force_epg_resync=_checkbox(data.get('force_epg_resync')),
        restart_schedule=restart_schedule,
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


@accounts_bp.route('/api/accounts/<int:account_id>/stream-check', methods=['POST'])
def stream_check_api(account_id):
    """Test a few of the account's channels now (app/stream_check.py). A check already
    running, or one owed after a first sync that has not happened, is reported rather than
    doubled; a waiting one starts now."""
    account, err = _json_account(account_id)
    if err:
        return err
    if account.status == ACCOUNT_STATUS_UNSYNCED:
        return jsonify({'error': f'"{account.name}" has no channels yet. Sync it first.'}), 409
    if account.status == ACCOUNT_STATUS_SYNCING:
        return jsonify({'error': f'"{account.name}" is syncing. Test it once the sync has '
                                 'finished.'}), 409
    check_id, message = stream_check.request_check(current_app._get_current_object(), account_id)
    if check_id is None:
        return jsonify({'error': message}), 404
    return jsonify({'success': True, 'message': message, 'check_id': check_id})


@accounts_bp.route('/api/accounts/sync-queue', methods=['GET'])
def sync_queue_status_api():
    """What the confirm needs before anyone clicks: every account in the Accounts list's order,
    where a queue stands, what would hold a new one back right now, and how long it pauses
    between accounts."""
    from .. import sync_queue
    from ..accounts import sync_conflicts
    accounts = [{'id': account_id, 'name': name} for account_id, name in
                db.session.query(Account.id, Account.name).order_by(Account.created_at)]
    names = {a['id']: a['name'] for a in accounts}
    queue = sync_queue.status()
    sentence = None
    if queue is not None:
        sentence = sync_queue.describe(
            queue, lambda account_id: names.get(account_id, f'account {account_id}'))
    return jsonify({'success': True, 'accounts': accounts, 'queue': queue, 'sentence': sentence,
                    'conflicts': sync_conflicts(None, other_syncs=False),
                    'pause_seconds': sync_queue.PAUSE_SECONDS})


@accounts_bp.route('/api/accounts/sync-queue', methods=['POST'])
def sync_queue_api():
    """Sync several accounts one at a time. Body: {account_ids, override?}. `override` is
    "Sync anyway": recordings and health checks stop holding the queue back. Without it the
    queue waits them out, so a conflict here is never a refusal (app/sync_queue.py)."""
    from .. import sync_queue
    data = request.get_json(silent=True) or {}
    raw = data.get('account_ids')
    if (not isinstance(raw, list) or not raw
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in raw)):
        return jsonify({'error': 'account_ids must be a list of account ids'}), 400
    account_ids = list(dict.fromkeys(raw))
    found = {row[0] for row in db.session.query(Account.id).filter(Account.id.in_(account_ids))}
    missing = [i for i in account_ids if i not in found]
    if missing:
        return jsonify({'error': f'No such account: {missing[0]}'}), 404
    started, message = sync_queue.start(account_ids, override=_checkbox(data.get('override')))
    if not started:
        return jsonify({'error': message}), 409
    return jsonify({'success': True, 'message': message})


@accounts_bp.route('/api/accounts/sync-queue/stop', methods=['POST'])
def sync_queue_stop_api():
    from .. import sync_queue
    return jsonify({'success': True, 'message': sync_queue.stop()})


@accounts_bp.route('/api/accounts/<int:account_id>/sync/cancel', methods=['POST'])
def cancel_sync_api(account_id):
    account, err = _json_account(account_id)
    if err:
        return err
    return jsonify({'success': True, 'message': _cancel_sync(account)})


# ── EPG sources on the account page (DESIGN-epg-sources.md §9.2) ──────────────────

def _json_owned_source(account_id, source_id, *, url_only=True, provider_ok=False):
    """(account, source, error_response) for a source this account owns. Adding, editing,
    deleting and refreshing are the owner's (§3); a provider source is edited only by
    editing its account. `provider_ok` also accepts a source the account's provider owns
    (DESIGN-epg-sources.md §15.2), for what any of its accounts may do with it: refresh it,
    use it again."""
    account, err = _json_account(account_id)
    if err:
        return None, None, err
    source = db.session.get(EpgSource, source_id)
    theirs = (provider_ok and source is not None and account.provider_id is not None
              and source.owner_provider_id == account.provider_id)
    if source is None or (source.owner_account_id != account_id and not theirs):
        return None, None, (jsonify({'error': 'EPG source not found on this account.'}), 404)
    if url_only and source.kind != EPG_SOURCE_URL:
        return None, None, (jsonify({'error': "The provider's guide comes with the account - "
                                              'edit the account instead.'}), 400)
    return account, source, None


def _case_sensitive() -> bool:
    return case_sensitive_matching(load_config())


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
    update_source_expiry_alerts(source_id)
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
        'enabled': bool(source.enabled),
        'expires_on': source.expires_on.isoformat() if source.expires_on else ''}})


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
    update_source_expiry_alerts(source_id)
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
    name = source.name
    readers = foreign_readers([source_id], account_id)
    # Its own refresh would go on writing rows for a source that no longer exists, and the
    # re-resolve of every channel it guided races an import of any source their accounts read.
    busy = refreshing_source_names([account_id] + [r.account_id for r in readers], [source_id])
    if busy:
        return jsonify({'error': refresh_busy_message(
            busy, 'Delete it once the refresh has finished.')}), 409
    guided_elsewhere = foreign_guided_channels([source_id], account_id)
    from ..scheduler import remove_epg_source_jobs
    remove_epg_source_jobs(source_id)
    done = delete_url_source(source_id, _case_sensitive())
    resolve_source_alerts(source_id)
    recheck_soon(f'EPG source #{source_id} deleted')
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
    _account, source, err = _json_owned_source(account_id, source_id, provider_ok=True)
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
    busy = refreshing_source_names([account_id], [source_id])
    if busy:
        return jsonify({'error': refresh_busy_message(busy)}), 409
    confirmed = bool((request.get_json(silent=True) or {}).get('confirm'))
    try:
        done = stop_using_source(account_id, source_id, _case_sensitive(),
                                 own_order_ok=confirmed)
    except NeedsOwnOrder as exc:
        return _own_order_refusal(exc)
    if done is None:
        return jsonify({'error': 'This account is not using that source.'}), 400
    recheck_soon(f'account #{account_id} stopped using EPG source #{source_id}')
    update_source_expiry_alerts(source_id)
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
    _account, source, err = _json_owned_source(account_id, source_id, url_only=False,
                                               provider_ok=True)
    if err:
        return err

    @retry_on_locked()
    def _subscribe_and_commit():
        added = use_source_again(account_id, source_id)
        db.session.commit()
        return added

    if not _subscribe_and_commit():
        return jsonify({'error': 'This account already uses that source.'}), 400
    recheck_soon(f'account #{account_id} uses EPG source #{source_id} again')
    update_source_expiry_alerts(source_id)
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
    data = request.get_json(silent=True) or {}
    direction = data.get('direction')
    if direction not in ('up', 'down'):
        return jsonify({'error': 'direction must be "up" or "down"'}), 400
    # An import resolving these channels at the same time would race the moves.
    busy = refreshing_source_names([account_id])
    if busy:
        return jsonify({'error': refresh_busy_message(busy)}), 409
    try:
        done = move_source(account_id, source_id, -1 if direction == 'up' else 1,
                           _case_sensitive(), own_order_ok=bool(data.get('confirm')))
    except NeedsOwnOrder as exc:
        return _own_order_refusal(exc)
    if done is None:
        return jsonify({'error': 'This account is not using that source.'}), 400
    if done.moved:
        recheck_soon(f'EPG source #{source_id} moved on account #{account_id}')
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
    own = ' This account has its own EPG order now.' if done.went_own else ''
    return jsonify({'success': True, 'switched': done.switched,
                    'message': f'Moved "{name}" {direction}. {switched}{own}'})


def _preview_answer(trial):
    """The JSON for a reorder preview (DESIGN-epg-sources.md §15.3): what the action would do
    to each channel's guide, worked out without doing it."""
    if trial is None:
        return jsonify({'error': 'This account is not using that source.'}), 400
    return jsonify(preview_json(trial, _case_sensitive()))


@accounts_bp.route('/api/accounts/<int:account_id>/epg-sources/<int:source_id>/move-preview')
def move_epg_source_preview_api(account_id, source_id):
    """What Move up / Move down would change (?direction=up|down). Reads only."""
    _account, err = _json_account(account_id)
    if err:
        return err
    direction = request.args.get('direction')
    if direction not in ('up', 'down'):
        return jsonify({'error': 'direction must be "up" or "down"'}), 400
    return _preview_answer(move_trial(account_id, source_id, -1 if direction == 'up' else 1))


@accounts_bp.route('/api/accounts/<int:account_id>/epg-sources/<int:source_id>/stop-using-preview')
def stop_using_epg_source_preview_api(account_id, source_id):
    """What Stop using would change. Reads only."""
    _account, err = _json_account(account_id)
    if err:
        return err
    return _preview_answer(stop_trial(account_id, source_id))


@accounts_bp.route('/api/accounts/<int:account_id>/epg-order/follow-preview')
def follow_provider_order_preview_api(account_id):
    """What "Follow <provider>'s order again" would change. Reads only."""
    _account, err = _json_account(account_id)
    if err:
        return err
    trial = follow_trial(account_id)
    if trial is None:
        return jsonify({'error': 'This account is not on a provider.'}), 400
    return _preview_answer(trial)


def _own_order_refusal(exc):
    """409 with the sentence the confirmation shows (DESIGN-epg-sources.md §15.3): the
    action would give a following account its own order, and goes ahead once the client
    sends `confirm`. The server refuses; the flag only says the sentence was shown."""
    return jsonify({'error': str(exc), 'confirm_required': {'message': str(exc)}}), 409


@accounts_bp.route('/api/accounts/<int:account_id>/epg-order/follow', methods=['POST'])
def follow_provider_order_api(account_id):
    """"Follow <provider>'s order again" (§15.3): the provider's list re-applied, any of
    its sources this account dropped read again, the account's own sources after."""
    account, err = _json_account(account_id)
    if err:
        return err
    view = account_order_view(account)
    if view is None:
        return jsonify({'error': 'This account is not on a provider.'}), 400
    if view['follows']:
        return jsonify({'error': f'This account already follows {view["provider_name"]}\'s '
                                 'order.'}), 400
    busy = refreshing_source_names([account_id])
    if busy:
        return jsonify({'error': refresh_busy_message(busy)}), 409
    switched = follow_provider_again(account_id, _case_sensitive())
    recheck_soon(f'account #{account_id} follows its provider\'s EPG order again')
    return jsonify({'success': True, 'switched': switched,
                    'message': (f'Following {view["provider_name"]}\'s order again. '
                                f'{_switched_sentence(switched)}')})


def _switched_sentence(n: int) -> str:
    if n == 1:
        return '1 channel now takes its guide from a different source.'
    if n:
        return f'{n:,} channels now take their guide from a different source.'
    return 'No channel changed where its guide comes from.'


@accounts_bp.route('/api/accounts/<int:account_id>/epg-sources/<int:source_id>/move-to-provider',
                   methods=['POST'])
def move_epg_source_to_provider_api(account_id, source_id):
    """"Move to <provider>" (§15.4): this account's url source becomes its provider's and
    every account on the provider reads it, with what the database holds copied at once.
    Body {refresh_interval_hours} when the source refreshed with this account's sync - a
    provider's source has no sync to ride (§15.2)."""
    account, source, err = _json_owned_source(account_id, source_id)
    if err:
        return err
    view = account_order_view(account)
    if view is None:
        return jsonify({'error': 'This account is not on a provider.'}), 400
    if source.is_account_guide:
        return jsonify({'error': f'"{source.name}" is this account\'s own guide, which fills '
                                 f'{view["provider_name"]}\'s "Provider\'s EPG" slot. Make '
                                 'another source its own guide first.'}), 400
    interval = source.refresh_interval_hours
    if not interval:
        raw = (request.get_json(silent=True) or {}).get('refresh_interval_hours')
        try:
            interval = int(raw)
        except (TypeError, ValueError):
            interval = None
        if interval not in REFRESH_HOURS_CHOICES:
            return jsonify({'error': 'Choose how often to refresh it: a provider\'s source has '
                                     'no account sync to refresh with.'}), 400
    busy = refreshing_source_names(provider_account_ids(view['provider_id']), [source_id])
    if busy:
        return jsonify({'error': refresh_busy_message(busy)}), 409
    done = move_source_to_provider(source_id, view['provider_id'], interval, _case_sensitive())
    from ..scheduler import schedule_epg_source_refresh
    schedule_epg_source_refresh(current_app._get_current_object(), source_id,
                                force_reschedule=True)
    recheck_soon(f'EPG source #{source_id} moved to provider #{view["provider_id"]}')
    return jsonify({'success': True, 'message': (
        f'"{source.name}" belongs to {view["provider_name"]} now, and '
        f'{done.readers} {"account reads" if done.readers == 1 else "accounts read"} it. '
        f'Nothing was downloaded again. {_switched_sentence(done.switched)}')})


@accounts_bp.route('/api/accounts/<int:account_id>/epg-sources/<int:source_id>/account-guide',
                   methods=['POST'])
def set_account_guide_api(account_id, source_id):
    """Make one of an M3U account's url sources its own guide (§15.3), the one that fills
    its provider's "Provider's EPG" slot."""
    account, source, err = _json_owned_source(account_id, source_id)
    if err:
        return err
    if account.account_type == 'xtream':
        return jsonify({'error': "An Xtream account's own guide is its provider's guide."}), 400
    busy = refreshing_source_names([account_id])
    if busy:
        return jsonify({'error': refresh_busy_message(busy)}), 409
    switched = set_account_guide(account_id, source_id, _case_sensitive())
    recheck_soon(f'account #{account_id} own guide set to EPG source #{source_id}')
    return jsonify({'success': True, 'message': (
        f'"{source.name}" is this account\'s own guide now. {_switched_sentence(switched)}')})


def _review_owner(source, only_account):
    """The account the review page goes back to: the source's own account, or for a
    provider's source (§15.2) the account the page was opened for, else the first account
    reading it. None when no account is on that provider any more."""
    if source.owner_account_id is not None:
        return db.session.get(Account, source.owner_account_id)
    readers = sorted(subscriber_ids(source.id))
    on_provider = [aid for (aid,) in db.session.query(Account.id).filter(
        Account.provider_id == source.owner_provider_id).order_by(Account.id)]
    for aid in ([only_account] if only_account in on_provider else []) + readers + on_provider:
        if aid in on_provider:
            return db.session.get(Account, aid)
    return None


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


@accounts_bp.route('/api/accounts/<int:account_id>/epg-scorecard', methods=['GET'])
def epg_scorecard_api(account_id):
    """The account's EPG source scorecard (app/epg_scorecard.py, dev/changelog/1717). Read
    only; nothing is fetched.

    `?view=card` is what the account page loads after it draws: each source's line and
    tooltip and the by-day count of the account's guided channels, as text. It is read
    after the page because it costs 0.6 s on prod's two largest accounts, almost all of it
    reading each guide's whole directory. Without it, every number unformatted, with each
    channel two or more sources cover, the source that serves it best and the name matches
    the review page would propose (another 2.9 s on account 5), and the followed sports
    games each source lists, as the EPG check stored them (dev/changelog/1726)."""
    _account, err = _json_account(account_id)
    if err:
        return err
    from ..epg_scorecard import (account_scorecard, card_json, name_match_counts,
                                 scorecard_json, sports_json)
    from ..epg_sources import last_local_day, to_local_date
    tz, now, cs = get_display_tz(), datetime.utcnow(), _case_sensitive()
    if request.args.get('view') == 'card':
        card = account_scorecard(account_id, tz, cs)
        return jsonify({'success': True,
                        **card_json(card, lambda day: date_label(day, now, tz),
                                    to_local_date(now, tz))})
    card = account_scorecard(account_id, tz, cs, with_channels=True,
                             day_of=lambda h: date_label(last_local_day(h, tz), now, tz))
    names = name_match_counts(account_id, [s.source_id for s in card.sources], cs)
    return jsonify({'success': True, **scorecard_json(card, names),
                    'sports': sports_json(account_id, now)})


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
    busy = refreshing_source_names([account_id], [source.id])
    if busy:
        return jsonify({'error': refresh_busy_message(busy)}), 409
    owner_name = owner_label(source) or 'its account'
    done = subscribe_to_source(account_id, source.id, _case_sensitive())
    if done is None:
        return jsonify({'error': 'This account already uses that source.'}), 400
    recheck_soon(f'account #{account_id} reads EPG source #{source.id}')
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
_REVIEW_VIEWS = ('proposals', 'disagreements', 'rejected', 'shared')


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
            'summary': directory_row_summary(entry.entry_count, entry.distinct_titles),
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
    only_account = request.args.get('account', type=int)
    page = max(request.args.get('page', 1, type=int) or 1, 1)

    result, new, disagree = _review_proposals(source, include_single, unguided_only,
                                              only_account)
    rejected = rejected_name_matches(source_id)
    # One id shared by channels whose names disagree (dev/changelog/1716); counted for
    # the tab on every view, so it is computed whichever view is shown.
    shared = shared_ids([source_id], subscriber_ids(source_id), _case_sensitive())
    listed = {'proposals': new, 'disagreements': disagree, 'rejected': rejected,
              'shared': shared}[view]
    pages = max((len(listed) + _REVIEW_PER_PAGE - 1) // _REVIEW_PER_PAGE, 1)
    page = min(page, pages)
    shown = listed[(page - 1) * _REVIEW_PER_PAGE:page * _REVIEW_PER_PAGE]

    account_names = dict(db.session.query(Account.id, Account.name))
    source_names = dict(db.session.query(EpgSource.id, EpgSource.name))
    tz, h24 = get_display_tz(), is_24h()
    rows = []
    for item in shown:
        if view == 'shared':
            rows.append(_shared_row_view(item, account_names, source_names, tz, h24))
            continue
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
            and [comparison_title(u['title']) for u in row['file']['upcoming']]
            == [comparison_title(u['title']) for u in row['current']['upcoming']])
        rows.append(row)

    owner = _review_owner(source, only_account)
    has_upcoming = any(r.get('file', {}).get('upcoming') for r in rows)
    return render_template(
        'epg_source_review.html', source=source, owner=owner, view=view, rows=rows,
        page=page, pages=pages, total=len(listed), per_page=_REVIEW_PER_PAGE,
        counts={'proposals': len(new), 'disagreements': len(disagree),
                'rejected': len(rejected), 'shared': len(shared)},
        ambiguous=result.ambiguous, single_hidden=result.single_hidden,
        include_single=include_single, unguided_only=unguided_only,
        only_account=only_account,
        only_account_name=account_names.get(only_account) if only_account else None,
        refreshed=has_directory(source.id), has_upcoming=has_upcoming,
        # Refresh goes through an account's route; a provider's source no account is on any
        # more has none to go through.
        can_refresh=source.kind == EPG_SOURCE_URL and source.enabled and owner is not None)


def _review_proposals(source, include_single: bool, unguided_only: bool,
                      account_id: int | None = None):
    """(NameMatchResult, proposals, disagreements) as the review page lists them: the
    page and Accept all read this one filter, so what is accepted is what was listed.
    `account_id` keeps one reading account's channels, the set an account's finding counts
    (dev/changelog/1718)."""
    result = name_match_review(source, _case_sensitive(), include_single)
    mine = [p for p in result.proposals
            if account_id is None or p.channel.account_id == account_id]
    new = [p for p in mine if p.kind == MATCH_NEW]
    disagree = [p for p in mine if p.kind == MATCH_DISAGREE]
    if unguided_only:
        new = [p for p in new if p.channel.epg_source_id is None]
    return result, new, disagree


@accounts_bp.route('/api/epg-sources/<int:source_id>/name-matches', methods=['GET'])
def name_match_picks_api(source_id):
    """Every proposal the Proposals tab lists under `single` and `unguided`, as picks, for
    Accept all (dev/changelog/1718). The page counts them in its confirm and posts them in
    chunks to the accept endpoint, which checks each again: the ids a person was shown
    are what is applied, never a match worked out at write time (DESIGN-epg-sources.md
    §16)."""
    source, err = _review_source_or_error(source_id)
    if err:
        return err
    _result, new, _disagree = _review_proposals(source, request.args.get('single') == '1',
                                                request.args.get('unguided') == '1',
                                                request.args.get('account', type=int))
    return jsonify({'success': True, 'picks': [
        {'channel_id': p.channel.id, 'xml_id': p.entry.xml_id} for p in new]})


def _shared_row_view(finding, account_names, source_names, tz, h24) -> dict:
    """One shared id on the review page: what the id lists, and the channels it misleads."""
    return {
        'account_id': finding.account_id,
        'account_name': account_names.get(finding.account_id, '?'),
        'file': _file_row_view(finding.entry, tz, h24),
        'guide_name': (finding.entry.display_names or [finding.entry.xml_id])[0],
        'total': len(finding.channels),
        'wrong': [{'id': ch.id, 'name': ch.name,
                   'guide_from': source_names.get(ch.epg_source_id)
                   if ch.epg_source_id else None} for ch in finding.wrong],
        'kept': [ch.name for ch in finding.named],
    }


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
    # Each accepted key writes the channel's rows as Set key does, for channels on every
    # account reading the source.
    busy = refreshing_source_names(subscriber_ids(source_id), [source_id])
    if busy:
        return jsonify({'error': refresh_busy_message(busy)}), 409
    done = accept_name_matches(source, picks, _case_sensitive())
    if done.applied:
        recheck_soon(f'name matches accepted on EPG source #{source_id}')
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
    if done.applied:
        recheck_soon(f'name matches rejected on EPG source #{source_id}')
    msg = f'Rejected {done.applied:,}. They will not be proposed again.'
    if done.stale:
        msg += f' {done.stale:,} were no longer proposed and were left alone.'
    return jsonify({'success': True, 'rejected': done.applied, 'stale': done.stale,
                    'message': msg})


@accounts_bp.route('/api/epg-sources/<int:source_id>/shared-ids/ignore', methods=['POST'])
def ignore_shared_id_api(source_id):
    """Body {account_id, xml_id, channel_ids}: stop using a shared id for the channels the
    page showed (DESIGN-epg-sources.md §16, dev/changelog/1716). Each id is checked against
    the finding as it stands now; the rest are counted as skipped."""
    source, err = _review_source_or_error(source_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        account_id = int(data.get('account_id'))
        ids = [int(c) for c in data.get('channel_ids') or []]
    except (TypeError, ValueError):
        return jsonify({'error': 'Bad request.'}), 400
    xml_id = str(data.get('xml_id') or '').strip()
    if not ids or not xml_id:
        return jsonify({'error': 'Nothing selected.'}), 400
    if account_id not in subscriber_ids(source_id):
        return jsonify({'error': "That account doesn't read this source."}), 400
    # Each channel's listings are deleted and its guide may move to another source of the
    # account, which an import of any of them would rewrite underneath.
    order = [s.source_id for s in EpgSourceSubscription.query.filter_by(account_id=account_id)]
    busy = refreshing_source_names([account_id], order)
    if busy:
        return jsonify({'error': refresh_busy_message(busy)}), 409
    done = ignore_shared_id(source_id, account_id, xml_id, ids, _case_sensitive())
    if done.applied:
        recheck_soon(f'a shared id stopped on EPG source #{source_id}')
    parts = [f'Stopped using {xml_id} for {done.applied:,} '
             f'channel{"s" if done.applied != 1 else ""}. Name matches may now suggest '
             'the right id for them.']
    if done.skipped:
        parts.append(f'{done.skipped:,} were no longer on it and were left alone.')
    return jsonify({'success': True, 'applied': done.applied, 'skipped': done.skipped,
                    'message': ' '.join(parts)})


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
    if n:
        recheck_soon(f'name matches proposed again on EPG source #{source_id}')
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


def _validate_account_form(form, *, new=False) -> list[str]:
    errors = []
    if not _field(form, 'name'):
        errors.append('Name is required.')
    account_type = form.get('account_type', 'm3u')
    if account_type == 'm3u':
        if not _field(form, 'm3u_url'):
            errors.append('M3U URL is required.')
    else:
        if not _field(form, 'base_url'):
            errors.append('Server is required.')
        if not _field(form, 'username'):
            errors.append('Username is required.')
        # Adding only: on an edit a blank password means "keep the stored one".
        if new and not form.get('password'):
            errors.append('Password is required.')
    max_conn_raw = _field(form, 'max_connections')
    if max_conn_raw and not (max_conn_raw.isdecimal() and int(max_conn_raw) >= 1):
        errors.append('Max connections must be a positive whole number.')
    sync_hours_raw = _field(form, 'sync_interval_hours')
    if sync_hours_raw and not (sync_hours_raw.isdecimal() and int(sync_hours_raw) >= 1):
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
