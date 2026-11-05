"""Syncing several accounts from one click: one at a time, with a pause between them.

A sync never overlaps another (app/admission.py), so "sync these three" cannot be three
requests: the second is refused while the first runs. The Readiness check's button was exactly
that, and it synced one account and reported an error (dev/docs/BUGS.md 2026-11-05).

**The queue is one one-shot job, re-armed for each account, holding the remaining accounts in
its kwargs.** That puts the work in the persistent jobstore, so it outlives the page that
started it and a service restart, /jobs shows it, and stopping it is removing the job. What is
only true while a step is running lives in `_state`, and is gone after a restart with nothing
lost: the next step sets it again.

**It yields where a scheduled sync yields, because nobody is watching it.** Before each account
it re-asks about recordings and health checks, and admission decides about other syncs. Blocked,
it says why and looks again a minute later rather than giving up. Starting it with `override`
is the user's "Sync anyway": recordings and health checks stop holding it back, another sync
still does, since overlapping those is the one thing this exists to prevent.

A failed account does not stop the rest. Its own sync history, its alert and the Readiness
check name the failure; this only logs it.
"""
import logging
import threading
from datetime import datetime, timedelta

from . import admission
from .fmt_utils import join_labels

log = logging.getLogger(__name__)

JOB_ID = 'account_sync_queue'

# Between one account finishing and the next starting. Accounts often share a provider
# backend, and each sync is a full playlist and guide download from it. It runs from the end of
# the sync's search index rebuild, which is the tail of _do_sync, so no account starts on top
# of the previous one's rebuild however long that takes. It stays longer than the delay on the
# auto-record rule check a guide import asks for, which yields to a sync and asks again after
# that same delay: a shorter pause would refuse the check all the way down the queue.
PAUSE_SECONDS = 60
# How soon to look again when something is holding the queue back.
WAIT_SECONDS = 60
# Armed while an account syncs, so the rest survive the process dying mid-sync. A normal step
# replaces it when the sync ends; it only ever fires after a crash or a forced restart.
RESUME_AFTER_SECONDS = 3600

_lock = threading.Lock()
_state = {}


def _set_state(**fields):
    with _lock:
        _state.update(fields)


def reset_for_tests() -> None:
    with _lock:
        _state.clear()


def _arm(pending, total, queued_at, override, delay_seconds):
    from .scheduler import _add_job
    _add_job(
        func=run_step,
        trigger='date',
        run_date=datetime.utcnow() + timedelta(seconds=delay_seconds),
        id=JOB_ID,
        replace_existing=True,
        kwargs={'pending': list(pending), 'total': total, 'queued_at': queued_at,
                'override': override},
    )


def status() -> dict | None:
    """Where the queue stands, or None when there is none. One jobstore read, nothing else."""
    from .scheduler import get_scheduler
    scheduler = get_scheduler()
    job = scheduler.get_job(JOB_ID) if scheduler is not None and scheduler.running else None
    with _lock:
        state = dict(_state)
    current = state.get('current')
    if job is None and current is None:
        return None
    kwargs = job.kwargs if job is not None else {}
    pending = list(kwargs.get('pending') or [])
    total = kwargs.get('total') or state.get('total') or len(pending)
    return {
        'current': current,
        'pending': pending,
        'total': total,
        'done': max(0, total - len(pending) - (1 if current is not None else 0)),
        'waiting': state.get('waiting'),
    }


def describe(queue: dict, name_of, show_waiting: bool = True) -> str:
    """`status()` as a sentence. `name_of(account_id)` names an account, and `show_waiting=False`
    leaves out the waiting reason, which can name a recording."""
    sentence = f"Syncing accounts one at a time, {queue['done']} of {queue['total']} done"
    if queue['current'] is not None:
        sentence += f", now {name_of(queue['current'])}"
    elif queue['waiting'] and show_waiting:
        sentence += f". Waiting: {queue['waiting'].rstrip('.')}"
    elif queue['waiting']:
        sentence += '. Waiting for other work to finish'
    if queue['pending']:
        sentence += f". Still to go: {join_labels(name_of(i) for i in queue['pending'])}"
    return sentence


def start(account_ids, *, override: bool = False):
    """Queue these accounts, in this order. Returns (started, message)."""
    from .fmt_utils import counted
    from .scheduler import scheduler_is_live
    if not scheduler_is_live():
        return False, ('The scheduler is not running, so the accounts cannot be queued. '
                       'The Readiness check says why.')
    if status() is not None:
        return False, 'Accounts are already syncing one at a time. Stop that first.'
    with _lock:
        _state.clear()
    _arm(account_ids, len(account_ids), datetime.utcnow(), override, 0)
    log.info('Sync queue: %d account(s) queued: %s%s', len(account_ids), account_ids,
             ' (overriding recordings and health checks)' if override else '')
    return True, (f'Syncing {counted(len(account_ids), "account")}, one at a time. '
                  'This keeps going if you leave this page.')


def stop() -> str:
    """Drop the accounts still waiting. One already syncing finishes: a sync stops only
    between its phases, and stopping it has its own control on the account page."""
    from .scheduler import remove_job_if_exists
    remove_job_if_exists(JOB_ID)
    with _lock:
        running = _state.get('current') is not None
        if running:
            _state['stopped'] = True
        else:
            _state.clear()
    log.info('Sync queue: stopped by the user')
    return ('Stopped. The account syncing now finishes, and the rest stay on their normal '
            'schedule.' if running else
            'Stopped. The remaining accounts stay on their normal schedule.')


def _hold(pending, total, queued_at, override, reason):
    """Something is in the way: say what, and look again."""
    log.info('Sync queue: holding %d account(s) - %s', len(pending), reason)
    _set_state(waiting=reason[0].upper() + reason[1:], total=total)
    _arm(pending, total, queued_at, override, WAIT_SECONDS)


def _finish(total):
    log.info('Sync queue: finished, %d account(s)', total)
    with _lock:
        _state.clear()


def _next_due(pending, queued_at):
    """Drop what no longer needs doing from the front of `pending`, and return that account
    with its id, or (None, None): a deleted account, or one a scheduled sync already caught
    up after the click."""
    from . import db
    from .database import Account
    while pending:
        account = db.session.get(Account, pending[0])
        if account is None:
            log.info('Sync queue: account %d no longer exists, skipping it', pending[0])
        elif account.last_sync_at is not None and account.last_sync_at >= queued_at:
            log.info('Sync queue: account %d has synced since it was queued, skipping it',
                     pending[0])
        else:
            return pending[0], account
        pending.pop(0)
    return None, None


def run_step(pending, total, queued_at, override=False):
    """One account. The scheduler job's body: re-arms itself until `pending` is empty."""
    from . import scheduler
    from .accounts import manual_sync_restarts_schedule, run_manual_sync, sync_conflicts
    from .config import load_config

    app = scheduler._app
    pending = list(pending)
    with app.app_context():
        account_id, account = _next_due(pending, queued_at)
        if account_id is None:
            _finish(total)
            return
        name = account.name
        restart_schedule = (account.sync_enabled
                            and manual_sync_restarts_schedule(load_config()))
        conflicts = [] if override else sync_conflicts(account_id, other_syncs=False)
    if conflicts:
        _hold(pending, total, queued_at, override, conflicts[0])
        return

    rest = pending[1:]
    if rest:
        _arm(rest, total, queued_at, override, RESUME_AFTER_SECONDS)
    _set_state(current=account_id, total=total, waiting=None)
    log.info('Sync queue: syncing "%s" (%d of %d)', name, total - len(rest), total)
    outcome = None
    try:
        outcome = run_manual_sync(
            app, account_id, restart_schedule=restart_schedule, force_admission=False,
            admission_ignoring=(admission.KIND_TESTER,) if override else ())
    except Exception as exc:
        # The rest of the queue is still wanted, exactly as _account_sync_job carries on.
        log.error('Sync queue: sync of account %d failed: %s', account_id, exc)
    finally:
        _set_state(current=None)

    with _lock:
        stopped = _state.get('stopped', False)
    if stopped:
        _finish(total)
    elif isinstance(outcome, admission.Refusal):
        _hold(pending, total, queued_at, override, outcome.reason)
    elif rest:
        _arm(rest, total, queued_at, override, PAUSE_SECONDS)
    else:
        _finish(total)
