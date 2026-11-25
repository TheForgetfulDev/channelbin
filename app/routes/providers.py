"""
Providers: the user's label for accounts that reach one backend, and the logins two of them
share (DESIGN-account-providers.md §6, dev/changelog/1170).

The section lives on the Accounts list (§6.2) and is rendered only when the app holds two or
more accounts. Every write goes through `app/account_links.py`, the one writer of
`Account.provider_id` and of the login lists; these routes validate the request, call it, and
say what happened.
"""
from datetime import datetime, timedelta

from flask import Blueprint, current_app, jsonify, request

from .. import db
from ..account_links import (configured_seats, create_provider_full, delete_provider_full,
                             hosts_for_accounts, logins_for_accounts, move_account_provider,
                             rename_provider, share_login, unshare_login)
from ..database import Account, EpgSource, Login, Provider
from ..db_utils import retry_on_locked
from ..epg_sources import REFRESH_HOURS_CHOICES
from ..fmt_utils import join_labels, plural

providers_bp = Blueprint('providers', __name__)


# ── Views ───────────────────────────────────────────────────────────────────────────────

def _holders_by_login(logins_by_account: dict) -> dict:
    """{login_id: [account_id, ...]} from rows already loaded for every account."""
    out: dict = {}
    for account_id, logins in logins_by_account.items():
        for lg in logins:
            out.setdefault(lg.id, []).append(account_id)
    for holders in out.values():
        holders.sort()
    return out


def providers_section(accounts) -> dict | None:
    """The Accounts page's Providers section, or None when there are fewer than two
    accounts - decided from the list the page already loaded, so a one-account user costs
    no query at all (§6.2). Otherwise one query each for providers, logins and hosts,
    however many accounts and providers there are."""
    from .. import connection_limits as connlim
    if len(accounts) < 2:
        return None
    providers = Provider.query.order_by(db.func.lower(Provider.name), Provider.id).all()
    names = {a.id: a.name for a in accounts}
    choices = [{'id': a.id, 'name': a.name,
                'provider': next((p.name for p in providers if p.id == a.provider_id), None)}
               for a in sorted(accounts, key=lambda a: a.name.lower())]
    if not providers:
        return {'providers': [], 'choices': choices, 'source_hours': list(REFRESH_HOURS_CHOICES)}
    on_provider = [a for a in accounts if a.provider_id is not None]
    ids = [a.id for a in on_provider]
    # Logins are read for EVERY account, not only the ones on a provider: a share can reach
    # an account on none (kept through a provider's deletion), and a holder map built from
    # the members alone would draw that login as unshared and offer it to the card's other
    # members as if it were the member's own (dev/changelog/1243). One query either way.
    logins_by_account = logins_for_accounts(a.id for a in accounts)
    hosts_by_account = hosts_for_accounts(ids)
    holders = _holders_by_login(logins_by_account)
    counts = connlim.login_pool_counts(holders)
    from ..epg_sources import provider_lists_view
    from ..tz_utils import format_local, get_display_tz, is_24h
    tz, h24 = get_display_tz(), is_24h()
    lists = provider_lists_view([p.id for p in providers], tz=tz)
    for entries in lists.values():
        for e in entries:
            if e.get('last_success_at'):
                e['last_success_at'] = format_local(e['last_success_at'], tz=tz, h24=h24)
    cards = []
    for p in providers:
        members = sorted((a for a in on_provider if a.provider_id == p.id),
                         key=lambda a: a.name.lower())
        member_ids = {a.id for a in members}
        rows = []
        shared = {}
        for a in members:
            chips = []
            for lg in logins_by_account.get(a.id, []):
                with_ids = [h for h in holders.get(lg.id, []) if h != a.id]
                chips.append({
                    'id': lg.id, 'name': lg.name,
                    'shared': bool(with_ids),
                    'share_targets': [{'id': m.id, 'name': m.name} for m in members
                                      if m.id != a.id and m.id not in holders.get(lg.id, [])],
                })
                if with_ids:
                    shared[lg.id] = lg
            hosts = hosts_by_account.get(a.id, [])
            rows.append({
                'id': a.id, 'name': a.name, 'type': a.account_type or 'm3u',
                'channels': a.channel_count or 0, 'logins': chips,
                'hosts': len(hosts),
                'active_host': next((h.host for h in hosts if h.is_active), None),
            })
        pools = []
        seats = configured_seats(shared.values())
        for lg in sorted(shared.values(), key=lambda lg: lg.name.lower()):
            held = counts.get(lg.id, 0)
            pools.append({
                'name': lg.name, 'seats': seats[lg.id], 'held': held,
                'accounts': [names.get(h, f'account {h}') for h in holders.get(lg.id, [])],
                'holders': (connlim.describe_pool_holders(lg.id, account_id=0, names=names)
                            if held else ''),
                'outside': [names.get(h, f'account {h}') for h in holders.get(lg.id, [])
                            if h not in member_ids],
            })
        cards.append({
            'id': p.id, 'name': p.name, 'accounts': rows, 'pools': pools,
            'addable': [c for c in choices if c['id'] not in member_ids],
            # Its EPG list (DESIGN-epg-sources.md §15.4): the slot and its own sources.
            'epg': lists.get(p.id, []),
            'members': [{'id': a.id, 'name': a.name} for a in members],
        })
    return {'providers': cards, 'choices': choices, 'source_hours': list(REFRESH_HOURS_CHOICES)}


def account_provider(account) -> dict | None:
    """The account page's Details line: {'id', 'name'} of its provider, or None."""
    if account.provider_id is None:
        return None
    provider = db.session.get(Provider, account.provider_id)
    return {'id': provider.id, 'name': provider.name} if provider else None


def login_shares(account_id: int, login_ids) -> dict:
    """{login_id: [other account names holding it]} for the account page's Logins card -
    a shared login says who else counts its seats. Two queries, none with no logins."""
    from ..account_links import login_holders
    if not login_ids:
        return {}
    holders = login_holders(login_ids)
    others = {h for hs in holders.values() for h in hs if h != account_id}
    if not others:
        return {}
    names = dict(db.session.query(Account.id, Account.name).filter(Account.id.in_(others)).all())
    return {lid: [names.get(h, f'account {h}') for h in hs if h != account_id]
            for lid, hs in holders.items() if any(h != account_id for h in hs)}


def sibling_blocks(account_id: int) -> dict:
    """What a block reaches across a shared login, for the account page's banners
    (design §5.3):

    - `covers`: [{'account', 'logins'}] - the accounts sharing a login with this one, so
      this account's own banner can say its block covers them too;
    - `covered_by`: [{'account_id', 'account', 'logins', 'blocks'}] - the siblings with a
      block in force or still to come, so this account's page says it is reached by a block
      the user set somewhere else.

    Nothing at all is queried past the sibling lookup for an account with no shared login."""
    from ..account_blocks import block_views
    from ..account_links import login_siblings
    found = login_siblings([account_id]).get(account_id) or {}
    if not found:
        return {'covers': [], 'covered_by': []}
    names = dict(db.session.query(Account.id, Account.name).filter(Account.id.in_(found)).all())
    login_names = dict(db.session.query(Login.id, Login.name).filter(
        Login.id.in_({lid for lids in found.values() for lid in lids})).all())
    views = block_views(found)
    covers, covered_by = [], []
    for other in sorted(found, key=lambda o: names.get(o, '').lower()):
        logins = [login_names.get(lid, f'#{lid}') for lid in found[other]]
        name = names.get(other, f'account {other}')
        covers.append({'account': name, 'logins': logins})
        if views.get(other):
            covered_by.append({'account_id': other, 'account': name, 'logins': logins,
                               'blocks': views[other]})
    return {'covers': covers, 'covered_by': covered_by}


# ── Routes ──────────────────────────────────────────────────────────────────────────────

def _names(ids) -> str:
    rows = dict(db.session.query(Account.id, Account.name).filter(Account.id.in_(ids)).all())
    return join_labels(rows.get(i, f'account {i}') for i in ids)


def _dropped_sentence(account_name: str, dropped) -> str:
    if not dropped:
        return ''
    which = ', '.join(f'"{n}"' for n in dropped)
    return (f' {account_name} stopped sharing {plural(len(dropped), "login")} {which}, '
            f'which only accounts on one provider may share.')


def _account_ids(data) -> list:
    raw = data.get('account_ids') or []
    if not isinstance(raw, list):
        raise ValueError('account_ids must be a list.')
    try:
        return [int(a) for a in raw]
    except (TypeError, ValueError):
        raise ValueError('account_ids must be account ids.')


@providers_bp.route('/api/providers', methods=['POST'])
def create_provider_api():
    """Create a provider and put accounts on it. Body: `name`, `account_ids`."""
    data = request.get_json(silent=True) or {}
    try:
        ids = _account_ids(data)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    # Each account then follows the new provider's EPG order, which re-decides its channels.
    busy = _busy_accounts(ids)
    if busy:
        return busy
    try:
        provider, dropped, changes = create_provider_full(str(data.get('name') or ''), ids)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    message = f'Provider "{provider.name}" created'
    message += f' with {_names(ids)}.' if ids else '.'
    for account_id, gone in dropped.items():
        message += _dropped_sentence(_names([account_id]), gone)
    for account_id, change in changes.items():
        epg = _epg_join_sentence(provider.name, change)
        if epg:
            message += f' {_names([account_id])}:{epg}'
    return jsonify({'success': True, 'provider_id': provider.id, 'message': message})


@providers_bp.route('/api/providers/join-preview')
def provider_join_preview_api():
    """What putting accounts on a provider would do to their guides (DESIGN-epg-sources.md
    §15.5): `?account_ids=1,2`, and `provider_id` for an existing provider (none for one
    being created). Reads only."""
    try:
        ids = [int(a) for a in (request.args.get('account_ids') or '').split(',') if a.strip()]
        pid = request.args.get('provider_id', type=int)
    except ValueError:
        return jsonify({'error': 'account_ids must be account ids.'}), 400
    if pid is not None and db.session.get(Provider, pid) is None:
        return jsonify({'error': 'Provider not found.'}), 404
    from ..epg_sources import join_trial, preview_json
    return jsonify(preview_json(join_trial(ids, pid), _cs()))


@providers_bp.route('/api/providers/<int:provider_id>/rename', methods=['POST'])
def rename_provider_api(provider_id):
    data = request.get_json(silent=True) or {}
    try:
        provider = rename_provider(provider_id, str(data.get('name') or ''))
    except LookupError as exc:
        return jsonify({'error': str(exc)}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'success': True, 'message': f'Provider renamed to "{provider.name}".'})


@providers_bp.route('/api/providers/<int:provider_id>', methods=['DELETE'])
def delete_provider_api(provider_id):
    """Delete a provider. Its accounts' hosts and logins stay as they are, shared logins
    included. Body {keep_sources: bool}, required when it owns EPG sources and never
    assumed (DESIGN-epg-sources.md §15.5, §17 Q3): kept, each goes to the lowest-numbered
    account reading it and no guide changes; not kept, they are deleted."""
    data = request.get_json(silent=True) or {}
    if db.session.get(Provider, provider_id) is None:
        return jsonify({'error': 'Provider not found.'}), 404
    owns = db.session.query(EpgSource.id).filter(
        EpgSource.owner_provider_id == provider_id).first() is not None
    keep = data.get('keep_sources')
    if owns and not isinstance(keep, bool):
        return jsonify({'error': 'Say whether to keep its EPG sources (keep_sources).'}), 400
    owned = [sid for (sid,) in db.session.query(EpgSource.id).filter(
        EpgSource.owner_provider_id == provider_id)]
    busy = _busy(provider_id, owned) if owns else None
    if busy:
        return busy
    before = _removal_facts(owned) if owns and not keep else None
    try:
        name, count, release = delete_provider_full(provider_id, bool(keep) if owns else True)
    except LookupError as exc:
        return jsonify({'error': str(exc)}), 404
    _forget_deleted_sources(release.deleted)
    if before is not None:
        _report_removed(before, release.deleted, f'with the provider "{name}"')
    message = (f'Provider "{name}" deleted; {count} {plural(count, "account")} no longer on it.')
    for source_name, account_name in release.handed:
        message += f' EPG source "{source_name}" belongs to {account_name} now.'
    if release.deleted:
        message += (f' {plural(len(release.deleted), "EPG source")} deleted'
                    + (f': {_removal_clause(release.removed)}' if release.removed.affected
                       else '.'))
    return jsonify({'success': True, 'message': message})


def _removal_facts(source_ids):
    """What EPG_SOURCE_REMOVED needs, read before a delete clears it (DESIGN-epg-sources.md
    §9.5, dev/changelog/763): each source's name, every account reading it with its guided
    channels, and {channel_id: source_id} for the channels it guides."""
    from ..epg_sources import foreign_guided_channels, foreign_readers
    ids = list(source_ids)
    names = dict(db.session.query(EpgSource.id, EpgSource.name).filter(EpgSource.id.in_(ids))) \
        if ids else {}
    # Owner id 0 is no account: every reader counts, since a provider's source has no owner
    # account to leave out.
    return names, foreign_readers(ids, 0), foreign_guided_channels(ids, 0)


def _report_removed(facts, deleted, cause: str) -> None:
    from ..accounts import report_source_removed
    from ..database import Channel
    names, readers, guided = facts
    outcome = dict(db.session.query(Channel.id, Channel.epg_source_id).filter(
        Channel.id.in_(list(guided)))) if guided else {}
    for sid in deleted:
        if sid in names:
            report_source_removed(sid, names[sid], cause, readers, guided, outcome)


def _forget_deleted_sources(source_ids) -> None:
    """A deleted source's refresh job and standing alerts go with it, and the findings of
    the accounts that read it are re-found."""
    if not source_ids:
        return
    from ..accounts import resolve_source_alerts
    from ..epg_scorecard import recheck_soon
    from ..scheduler import remove_epg_source_jobs
    for sid in source_ids:
        remove_epg_source_jobs(sid)
        resolve_source_alerts(sid)
    recheck_soon('provider EPG sources deleted')


def _removal_clause(done) -> str:
    parts = []
    if done.switched:
        parts.append(f'{done.switched:,} {plural(done.switched, "channel")} now take their '
                     'guide from another source')
    if done.lost:
        parts.append(f'{done.lost:,} have no guide from any source now')
    return ', and '.join(parts) + '.' if parts else 'no channel took its guide from them.'


def _epg_join_sentence(provider_name: str, change) -> str:
    if change is None:
        return ''
    out = ''
    if change.removed is not None and change.removed.affected:
        out += f' It stopped using its old provider\'s EPG sources: {_removal_clause(change.removed)}'
    if change.switched:
        out += (f' It follows {provider_name}\'s EPG order: {change.switched:,} '
                f'{plural(change.switched, "channel")} changed where the guide comes from.')
    return out


@providers_bp.route('/api/providers/<int:provider_id>/accounts', methods=['POST'])
def add_provider_account_api(provider_id):
    """Put an account on this provider. Body: `account_id`. An account moving from another
    provider stops sharing the logins it shared there."""
    data = request.get_json(silent=True) or {}
    try:
        account_id = int(data.get('account_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'account_id is required.'}), 400
    provider = db.session.get(Provider, provider_id)
    if provider is None:
        return jsonify({'error': 'Provider not found.'}), 404
    name = provider.name
    # Joining follows the provider's EPG order, which re-decides the account's channels; an
    # import of any source it reads would race those moves (dev/changelog/1298).
    busy = _busy_accounts([account_id])
    if busy:
        return busy
    try:
        dropped, change = move_account_provider(account_id, provider_id)
    except LookupError as exc:
        return jsonify({'error': str(exc)}), 404
    account_name = _names([account_id])
    return jsonify({'success': True,
                    'message': (f'{account_name} is on {name} now.'
                                + _dropped_sentence(account_name, dropped)
                                + _epg_join_sentence(name, change))})


@providers_bp.route('/api/providers/<int:provider_id>/accounts/<int:account_id>', methods=['DELETE'])
def remove_provider_account_api(provider_id, account_id):
    """Take an account off this provider. It stops sharing any login another account on
    the provider still holds; logins only it holds stay."""
    account = db.session.get(Account, account_id)
    provider = db.session.get(Provider, provider_id)
    if account is None or provider is None or account.provider_id != provider_id:
        return jsonify({'error': 'That account is not on this provider.'}), 404
    account_name, provider_name = account.name, provider.name
    from ..epg_sources import refresh_busy_message, refreshing_source_names
    busy = refreshing_source_names([account_id])
    if busy:
        return jsonify({'error': refresh_busy_message(busy)}), 409
    dropped, change = move_account_provider(account_id, None)
    removed = change.removed if change is not None else None
    epg = (f' It stopped using {provider_name}\'s EPG sources: {_removal_clause(removed)}'
           if removed is not None and removed.affected else '')
    return jsonify({'success': True,
                    'message': (f'{account_name} is no longer on {provider_name}.'
                                + _dropped_sentence(account_name, dropped) + epg)})


@providers_bp.route('/api/logins/<int:login_id>/share', methods=['POST'])
def share_login_api(login_id):
    """Add a login to another account on the same provider. Body: `account_id`."""
    data = request.get_json(silent=True) or {}
    try:
        account_id = int(data.get('account_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'account_id is required.'}), 400
    try:
        login, replaced = share_login(login_id, account_id)
    except LookupError as exc:
        return jsonify({'error': str(exc)}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    seats = configured_seats([login])[login.id]
    target = _names([account_id])
    message = (f'Login "{login.name}" is shared with {target}. Its {seats} '
               f'{plural(seats, "seat")} now {plural(seats, "counts", "count")} across '
               f'both accounts.')
    if replaced is not None:
        message += (f' {target} listed the same username and password from its own '
                    f'playlist; the shared login took that row\'s place.')
    return jsonify({'success': True, 'message': message})


@providers_bp.route('/api/logins/<int:login_id>/share/<int:account_id>', methods=['DELETE'])
def unshare_login_api(login_id, account_id):
    """`account_id` stops holding a shared login; the other account keeps it."""
    account_name = _names([account_id])
    try:
        name = unshare_login(login_id, account_id)
    except LookupError as exc:
        return jsonify({'error': str(exc)}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    return jsonify({'success': True,
                    'message': f'{account_name} no longer holds login "{name}".'})


# ── The provider's EPG list (DESIGN-epg-sources.md §15) ─────────────────────────────────
#
# The writes are app/epg_sources.py's; every one that re-decides channels first asks whether
# a source refresh is in flight for any account on the provider, as the account page's own
# source actions do (dev/changelog/1298).

def _provider_or_404(provider_id):
    provider = db.session.get(Provider, provider_id)
    if provider is None:
        return None, (jsonify({'error': 'Provider not found.'}), 404)
    return provider, None


def _provider_source_or_404(provider_id, source_id):
    provider, err = _provider_or_404(provider_id)
    if err:
        return None, None, err
    source = db.session.get(EpgSource, source_id)
    if source is None or source.owner_provider_id != provider_id:
        return None, None, (jsonify({'error': 'EPG source not found on this provider.'}), 404)
    return provider, source, None


def _busy(provider_id, source_ids=()):
    """409 when a refresh is in flight for any account on the provider or any account reading
    one of `source_ids` - another provider's account may read a source too (§15.2)."""
    from ..epg_sources import provider_account_ids, subscriber_ids
    accounts = set(provider_account_ids(provider_id))
    for sid in source_ids:
        accounts.update(subscriber_ids(sid))
    return _busy_accounts(sorted(accounts), source_ids)


def _busy_accounts(account_ids, source_ids=()):
    from ..epg_sources import refresh_busy_message, refreshing_source_names
    busy = refreshing_source_names(list(account_ids), list(source_ids))
    return (jsonify({'error': refresh_busy_message(busy)}), 409) if busy else None


def _cs() -> bool:
    from .accounts import _case_sensitive
    return _case_sensitive()


@providers_bp.route('/api/providers/<int:provider_id>/epg-sources', methods=['POST'])
def add_provider_epg_source_api(provider_id):
    """Add an XMLTV URL to the provider (§15.2): last on its list, read by every account on
    it, refreshed on its own interval and fetched once at once."""
    provider, err = _provider_or_404(provider_id)
    if err:
        return err
    from ..accounts import start_source_refresh
    from ..epg_sources import add_provider_source, clean_provider_source_fields
    errors, fields = clean_provider_source_fields(request.get_json(silent=True) or {},
                                                  provider.name)
    if errors:
        return jsonify({'error': ' '.join(errors)}), 400

    @retry_on_locked()
    def _add_and_commit():
        source, _added = add_provider_source(provider_id, fields)
        db.session.commit()
        return source.id

    source_id = _add_and_commit()
    from ..accounts import update_source_expiry_alerts
    update_source_expiry_alerts(source_id)
    app_obj = current_app._get_current_object()
    from ..epg_scorecard import recheck_soon
    from ..scheduler import schedule_epg_source_refresh
    started, message = start_source_refresh(app_obj, source_id)
    schedule_epg_source_refresh(
        app_obj, source_id,
        first_run=datetime.utcnow() + timedelta(hours=fields['refresh_interval_hours']))
    recheck_soon(f'EPG source #{source_id} added to provider #{provider_id}')
    return jsonify({'success': True, 'source_id': source_id, 'refresh_started': started,
                    'message': (f'Added "{fields["name"]}" to {provider.name}; every account '
                                f'on it reads it. {message}')})


@providers_bp.route('/api/providers/<int:provider_id>/epg-sources/<int:source_id>',
                    methods=['GET'])
def provider_epg_source_settings_api(provider_id, source_id):
    """The Edit source dialog's values, the URL in full as on the account page's."""
    _provider, source, err = _provider_source_or_404(provider_id, source_id)
    if err:
        return err
    return jsonify({'success': True, 'source': {
        'id': source.id, 'name': source.name, 'url': source.url or '',
        'refresh_interval_hours': source.refresh_interval_hours or '',
        'enabled': bool(source.enabled),
        'expires_on': source.expires_on.isoformat() if source.expires_on else ''}})


@providers_bp.route('/api/providers/<int:provider_id>/epg-sources/<int:source_id>',
                    methods=['POST'])
def save_provider_epg_source_api(provider_id, source_id):
    provider, source, err = _provider_source_or_404(provider_id, source_id)
    if err:
        return err
    from ..accounts import (start_source_refresh, update_source_expiry_alerts,
                            update_source_stale_alert)
    from ..epg_sources import clean_provider_source_fields, update_url_source
    errors, fields = clean_provider_source_fields(request.get_json(silent=True) or {},
                                                  provider.name)
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
        message += ' ' + start_source_refresh(app_obj, source_id)[1]
        schedule_epg_source_refresh(
            app_obj, source_id, force_reschedule=True,
            first_run=datetime.utcnow() + timedelta(hours=fields['refresh_interval_hours']))
    else:
        schedule_epg_source_refresh(app_obj, source_id, force_reschedule=True)
    return jsonify({'success': True, 'message': message})


@providers_bp.route('/api/providers/<int:provider_id>/epg-sources/<int:source_id>',
                    methods=['DELETE'])
def delete_provider_epg_source_api(provider_id, source_id):
    """Take a source off the provider's list by deleting it (§15.3): it goes from every
    account that reads it, own-order ones included, and each channel it was the guide for
    moves to its next source."""
    _provider, source, err = _provider_source_or_404(provider_id, source_id)
    if err:
        return err
    busy = _busy(provider_id, [source_id])
    if busy:
        return busy
    from ..epg_sources import delete_url_source
    name = source.name
    facts = _removal_facts([source_id])
    done = delete_url_source(source_id, _cs())
    _forget_deleted_sources([source_id])
    _report_removed(facts, [source_id], 'by hand on its provider\'s card')
    clause = _removal_clause(done) if done is not None and done.affected else \
        'No channel took its guide from it.'
    return jsonify({'success': True, 'message': f'Deleted "{name}". {clause}'})


@providers_bp.route('/api/providers/<int:provider_id>/epg-sources/<int:source_id>/refresh',
                    methods=['POST'])
def refresh_provider_epg_source_api(provider_id, source_id):
    _provider, source, err = _provider_source_or_404(provider_id, source_id)
    if err:
        return err
    if not source.enabled:
        return jsonify({'error': f'"{source.name}" is turned off. Turn it on in Edit '
                                 'first.'}), 400
    if source.refresh_started_at is not None:
        return jsonify({'error': f'"{source.name}" is already being refreshed.'}), 409
    from ..accounts import start_source_refresh
    started, message = start_source_refresh(current_app._get_current_object(), source_id)
    return jsonify({'success': True, 'refresh_started': started, 'message': message})


@providers_bp.route('/api/providers/<int:provider_id>/epg-sources/<int:source_id>/give',
                    methods=['POST'])
def give_provider_epg_source_api(provider_id, source_id):
    """Move a provider's source to one of its accounts (§15.3): it leaves the list and is
    that account's alone; every other account reading it stops. Body {account_id}."""
    provider, source, err = _provider_source_or_404(provider_id, source_id)
    if err:
        return err
    try:
        account_id = int((request.get_json(silent=True) or {}).get('account_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'account_id is required.'}), 400
    account = db.session.get(Account, account_id)
    if account is None or account.provider_id != provider_id:
        return jsonify({'error': f'That account is not on {provider.name}.'}), 404
    busy = _busy(provider_id, [source_id])
    if busy:
        return busy
    from ..epg_scorecard import recheck_soon
    from ..epg_sources import move_source_to_account
    name, account_name = source.name, account.name
    done = move_source_to_account(source_id, account_id, _cs())
    recheck_soon(f'EPG source #{source_id} moved to account #{account_id}')
    message = f'"{name}" belongs to {account_name} now, and only it reads it.'
    if done.affected:
        message += f' On the other accounts: {_removal_clause(done)}'
    return jsonify({'success': True, 'message': message})


@providers_bp.route('/api/providers/<int:provider_id>/epg-order/<int:entry_id>/move-preview')
def move_provider_epg_entry_preview_api(provider_id, entry_id):
    """What moving one entry of the provider's list would do to the guides of the accounts
    following it (?direction=up|down). Reads only."""
    _provider, err = _provider_or_404(provider_id)
    if err:
        return err
    direction = request.args.get('direction')
    if direction not in ('up', 'down'):
        return jsonify({'error': 'direction must be "up" or "down"'}), 400
    from ..epg_sources import preview_json, provider_move_trial
    trial = provider_move_trial(provider_id, entry_id, -1 if direction == 'up' else 1)
    if trial is None:
        return jsonify({'error': 'That entry is not on this provider\'s list.'}), 404
    return jsonify(preview_json(trial, _cs()))


@providers_bp.route('/api/providers/<int:provider_id>/epg-order/<int:entry_id>/move',
                    methods=['POST'])
def move_provider_epg_entry_api(provider_id, entry_id):
    """Body {direction: 'up' | 'down'}: one place on the provider's list (§15.3). Every
    account following its order is re-decided from listings already held; accounts on
    their own order are left alone."""
    provider, err = _provider_or_404(provider_id)
    if err:
        return err
    direction = (request.get_json(silent=True) or {}).get('direction')
    if direction not in ('up', 'down'):
        return jsonify({'error': 'direction must be "up" or "down"'}), 400
    busy = _busy(provider_id)
    if busy:
        return busy
    from ..epg_scorecard import recheck_soon
    from ..epg_sources import move_provider_entry
    done = move_provider_entry(provider_id, entry_id, -1 if direction == 'up' else 1, _cs())
    if done is None:
        return jsonify({'error': 'That entry is not on this provider\'s list.'}), 404
    if not done.moved:
        return jsonify({'success': True, 'message': (
            f'Already {"first" if direction == "up" else "last"} on {provider.name}\'s list.')})
    recheck_soon(f'provider #{provider_id} EPG list reordered')
    changed = (f'{done.switched:,} {plural(done.switched, "channel")} on the accounts following '
               'it now take their guide from a different source.' if done.switched
               else 'No channel changed where its guide comes from.')
    return jsonify({'success': True, 'switched': done.switched,
                    'message': f'Moved it {direction} on {provider.name}\'s list. {changed}'})


@providers_bp.route('/api/providers/<int:provider_id>/delete-preview', methods=['GET'])
def provider_delete_preview_api(provider_id):
    """What the delete dialog counts about the provider's EPG sources (§15.5)."""
    _provider, err = _provider_or_404(provider_id)
    if err:
        return err
    from ..epg_sources import provider_release_preview
    return jsonify({'success': True, **provider_release_preview(provider_id)})
