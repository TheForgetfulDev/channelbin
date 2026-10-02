"""
Providers: the user's label for accounts that reach one backend, and the logins two of them
share (DESIGN-account-providers.md §6, dev/changelog/1170).

The section lives on the Accounts list (§6.2) and is rendered only when the app holds two or
more accounts. Every write goes through `app/account_links.py`, the one writer of
`Account.provider_id` and of the login lists; these routes validate the request, call it, and
say what happened.
"""
from flask import Blueprint, jsonify, request

from .. import db
from ..account_links import (create_provider, delete_provider, hosts_for_accounts,
                             logins_for_accounts, rename_provider, set_account_provider,
                             share_login, unshare_login)
from ..database import Account, Login, Provider

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
        return {'providers': [], 'choices': choices}
    on_provider = [a for a in accounts if a.provider_id is not None]
    ids = [a.id for a in on_provider]
    logins_by_account = logins_for_accounts(ids)
    hosts_by_account = hosts_for_accounts(ids)
    holders = _holders_by_login(logins_by_account)
    counts = connlim.login_pool_counts(holders)
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
        for lg in sorted(shared.values(), key=lambda lg: lg.name.lower()):
            held = counts.get(lg.id, 0)
            pools.append({
                'name': lg.name, 'seats': lg.max_connections, 'held': held,
                'accounts': [names.get(h, f'account {h}') for h in holders.get(lg.id, [])],
                'holders': (connlim.describe_pool_holders(lg.id, account_id=0, names=names)
                            if held else ''),
                'outside': [names.get(h, f'account {h}') for h in holders.get(lg.id, [])
                            if h not in member_ids],
            })
        cards.append({
            'id': p.id, 'name': p.name, 'accounts': rows, 'pools': pools,
            'addable': [c for c in choices if c['id'] not in member_ids],
        })
    return {'providers': cards, 'choices': choices}


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
    listed = [rows.get(i, f'account {i}') for i in ids]
    if len(listed) <= 1:
        return ''.join(listed)
    return ', '.join(listed[:-1]) + ' and ' + listed[-1]


def _dropped_sentence(account_name: str, dropped) -> str:
    if not dropped:
        return ''
    which = ', '.join(f'"{n}"' for n in dropped)
    return (f' {account_name} stopped sharing login{"" if len(dropped) == 1 else "s"} {which}, '
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
        provider, dropped = create_provider(str(data.get('name') or ''), ids)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    message = f'Provider "{provider.name}" created'
    message += f' with {_names(ids)}.' if ids else '.'
    for account_id, gone in dropped.items():
        message += _dropped_sentence(_names([account_id]), gone)
    return jsonify({'success': True, 'provider_id': provider.id, 'message': message})


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
    included."""
    try:
        name, count = delete_provider(provider_id)
    except LookupError as exc:
        return jsonify({'error': str(exc)}), 404
    return jsonify({'success': True,
                    'message': (f'Provider "{name}" deleted; {count} account'
                                f'{"" if count == 1 else "s"} no longer on it.')})


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
    try:
        dropped = set_account_provider(account_id, provider_id)
    except LookupError as exc:
        return jsonify({'error': str(exc)}), 404
    account_name = _names([account_id])
    return jsonify({'success': True,
                    'message': (f'{account_name} is on {name} now.'
                                + _dropped_sentence(account_name, dropped))})


@providers_bp.route('/api/providers/<int:provider_id>/accounts/<int:account_id>', methods=['DELETE'])
def remove_provider_account_api(provider_id, account_id):
    """Take an account off this provider. It stops sharing any login another account on
    the provider still holds; logins only it holds stay."""
    account = db.session.get(Account, account_id)
    provider = db.session.get(Provider, provider_id)
    if account is None or provider is None or account.provider_id != provider_id:
        return jsonify({'error': 'That account is not on this provider.'}), 404
    account_name, provider_name = account.name, provider.name
    dropped = set_account_provider(account_id, None)
    return jsonify({'success': True,
                    'message': (f'{account_name} is no longer on {provider_name}.'
                                + _dropped_sentence(account_name, dropped))})


@providers_bp.route('/api/logins/<int:login_id>/share', methods=['POST'])
def share_login_api(login_id):
    """Add a login to another account on the same provider. Body: `account_id`."""
    data = request.get_json(silent=True) or {}
    try:
        account_id = int(data.get('account_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'account_id is required.'}), 400
    try:
        login = share_login(login_id, account_id)
    except LookupError as exc:
        return jsonify({'error': str(exc)}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    return jsonify({'success': True,
                    'message': (f'Login "{login.name}" is shared with {_names([account_id])}. '
                                f'Its {login.max_connections} seat'
                                f'{"" if login.max_connections == 1 else "s"} now count across '
                                f'both accounts.')})


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
