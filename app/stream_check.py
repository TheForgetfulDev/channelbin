"""An account's stream check: a few of its channels test-played, to show that its stream URLs
play at all (dev/changelog/1665).

A sync proves the provider answered with a channel list; nothing until now proved that the
URLs in it play, and a wrong URL Normalization mode or stream origin on an account whose URLs
ChannelBin built itself is wrong for every channel at once. This answers it at import time
instead of at the first recording.

**Most channels not playing proves nothing.** On a real IPTV account a large share of
channels is down at any given moment - pay-per-view and event channels most of all - so a
sample that all failed is often bad luck. Two things follow:

- **The sample leans toward channels likely to be on**: categories that read as local or
  network TV first, categories and names that read as pay-per-view or event channels last,
  and inside each, channels with guide listings in the next 24 hours first; one channel per
  category while there are categories left, at random inside all of that. `pick_channels()`.
- **The verdict is read from how each test failed, not from how many failed.** A refused
  login or a stream server that cannot be reached is the account's problem; a channel that
  did not load or served the provider's "channel offline" clip is that channel's.
  `classify()` names each test's outcome from the tester's raw signals, `judge()` turns the
  outcomes into a verdict, and only the account-level verdicts raise an alert.

It stops early once the answer is in: the first channel that plays proves the URLs work, and
two channels failing the same account-level way (refused, or the server not found) proves the
rest would only repeat it, without opening more connections to a provider that is refusing.

A check is one run of the Channel Tester (channel_tester.run_channel_sample), so it holds the
tester's one run slot, takes the account's seat for each test, and a starting recording
preempts it. When the tester is busy, a recording is about to start or the account is
blocked, the check WAITs and retries; the row says why and when. The row is the obligation:
it is written before anything runs and only DONE ends it, so a restart or a refusal can only
leave a check that is still owed (`recover_at_startup()`).
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Optional

from . import db
from .account_links import is_credential_refusal, is_resolution_failure
from .database import (Account, AccountStreamCheck, Channel, ChannelTest, EPGEntry,
                       STREAM_CHECK_DONE, STREAM_CHECK_OPEN, STREAM_CHECK_PENDING_SYNC,
                       STREAM_CHECK_RUNNING, STREAM_CHECK_WAITING, TEST_STATUS_CANCELLED,
                       ACCOUNT_STATUS_OK)
from .db_utils import retry_on_locked
from .fmt_utils import counted

log = logging.getLogger(__name__)

TRIGGER_FIRST_SYNC = 'first_sync'   # asked for at Add account, runs after the first good sync
TRIGGER_MANUAL = 'manual'           # the account page's "Test a few channels"

#: Most channels a check tests. It usually stops sooner - see the module docstring.
SAMPLE_SIZE = 5
#: Each test's length. The question is whether the stream plays at all, not how well, so a
#: short clip answers it; the health check's own length is left alone.
TEST_SECONDS = 10
#: The pause between two tests, so a one-connection provider has let the last one go.
WAIT_SECONDS = 5
#: One retry per channel, a few seconds on: a first connection that fails is often a fluke.
CONNECT_RETRIES = 1
RETRY_DELAY_SECONDS = 3
#: How long a check that could not start waits before it asks again.
RETRY_MINUTES = 10
#: Channels failing the same account-level way before the sample stops.
SAME_FAILURE_STOP = 2
#: How far ahead a channel's listings count toward it being on.
LISTINGS_HOURS = 24

# How one channel's test came out, in the order classify() asks.
OUT_NOT_RUN = 'not_run'             # the test never ran (no free seat, channel gone)
OUT_INTERRUPTED = 'interrupted'     # a recording took the seat, or Stop was pressed
OUT_OFFLINE = 'offline_clip'        # the provider served its "channel offline" clip
OUT_PLAYED = 'played'
OUT_REFUSED = 'refused'             # 401/403: the login was refused
OUT_NO_HOST = 'no_such_host'        # the stream server's name does not resolve
OUT_UNREACHABLE = 'unreachable'     # nothing answers at the stream server's address
OUT_NOT_FOUND = 'not_found'         # 404/410 for this stream
OUT_HTTP_ERROR = 'http_error'       # another HTTP error
OUT_NO_ANSWER = 'no_answer'         # connected or tried to, and nothing came back in time
OUT_NO_DATA = 'no_data'             # it ended without a picture, for no reason it named
OUTCOMES = (OUT_NOT_RUN, OUT_INTERRUPTED, OUT_OFFLINE, OUT_PLAYED, OUT_REFUSED, OUT_NO_HOST,
            OUT_UNREACHABLE, OUT_NOT_FOUND, OUT_HTTP_ERROR, OUT_NO_ANSWER, OUT_NO_DATA)
#: The outcomes that say something about the account rather than the channel.
OUT_ACCOUNT_LEVEL = (OUT_REFUSED, OUT_NO_HOST, OUT_UNREACHABLE)
#: The outcomes of a test that never got to answer anything.
OUT_UNTESTED = (OUT_NOT_RUN, OUT_INTERRUPTED)

OUTCOME_WORDS = {
    OUT_NOT_RUN: 'not tested',
    OUT_INTERRUPTED: 'interrupted before it finished',
    OUT_OFFLINE: 'the provider answered with its "channel offline" clip',
    OUT_PLAYED: 'played',
    OUT_REFUSED: 'the provider refused the login',
    OUT_NO_HOST: 'the stream server\'s name was not found',
    OUT_UNREACHABLE: 'nothing answered at the stream server\'s address',
    OUT_NOT_FOUND: 'the provider said this stream does not exist',
    OUT_HTTP_ERROR: 'the provider answered with an error',
    OUT_NO_ANSWER: 'no answer from the stream in time',
    OUT_NO_DATA: 'no picture came through',
}

VERDICT_WORKS = 'works'
VERDICT_LOGIN_OK = 'login_ok'                       # nothing played, but the server answered
VERDICT_LOGIN_REFUSED = 'login_refused'
VERDICT_SERVER_UNREACHABLE = 'server_unreachable'
VERDICT_URLS_NOT_FOUND = 'urls_not_found'
VERDICT_INCONCLUSIVE = 'inconclusive'
VERDICT_NO_CHANNELS = 'no_channels'
VERDICTS = (VERDICT_WORKS, VERDICT_LOGIN_OK, VERDICT_LOGIN_REFUSED, VERDICT_SERVER_UNREACHABLE,
            VERDICT_URLS_NOT_FOUND, VERDICT_INCONCLUSIVE, VERDICT_NO_CHANNELS)
#: The verdicts that mean the account itself is not working, and raise the alert.
VERDICT_ACCOUNT_PROBLEM = (VERDICT_LOGIN_REFUSED, VERDICT_SERVER_UNREACHABLE,
                           VERDICT_URLS_NOT_FOUND)

ALERT_TYPE = 'ACCOUNT_STREAM_CHECK_FAILED'

#: Categories and names that read as pay-per-view, event or package channels - the ones most
#: likely to be dark at any given moment - plus radio and VOD-like lists.
_UNLIKELY = re.compile(
    r'ppv|pay[\s-]*per[\s-]*view|\bevents?\b|sunday\s*ticket|league\s*pass|center\s*ice'
    r'|extra\s*innings|game\s*pass|espn\s*(\+|plus)|\bufc\b|boxing|\bwwe\b|\bdazn\b|\bflo\b'
    r'|replay|\bvod\b|24\s*/\s*7|adult|xxx|\bradio\b|\bmusic\b', re.IGNORECASE)
#: Categories that read as local or network TV, the ones most likely to be on.
_LIKELY = re.compile(
    r'\b(locals?|usa?|united\s+states|networks?|abc|nbc|cbs|fox|pbs|cw|news|entertainment'
    r'|general)\b', re.IGNORECASE)

_HTTP_ERROR = re.compile(r'server returned|http error', re.IGNORECASE)
_NOT_FOUND = re.compile(r'(server returned|http error) (404|410)', re.IGNORECASE)


# ── Choosing the channels (pure) ───────────────────────────────────────────────────────────

def category_rank(category: Optional[str]) -> int:
    """0 for a category that reads as local or network TV, 2 for one that reads as
    pay-per-view or events, 1 for everything else. "Unlikely" wins a tie: "USA | PPV" is
    a pay-per-view list whatever else its name says."""
    text = category or ''
    if _UNLIKELY.search(text):
        return 2
    if _LIKELY.search(text):
        return 0
    return 1


def pick_channels(candidates, rng: random.Random, k: int = SAMPLE_SIZE) -> list:
    """Up to `k` of `candidates` (dicts with 'id', 'name', 'category', 'has_listings'), best
    first: by category_rank() (a name that reads as an event channel ranks with the unlikely
    categories), then channels with listings ahead of channels without, one per category
    while there are categories left, at random inside each rank. Pure: the same candidates
    and seed give the same pick."""
    tiers = defaultdict(lambda: defaultdict(list))
    for c in candidates:
        rank = 2 if _UNLIKELY.search(c.get('name') or '') else category_rank(c.get('category'))
        tiers[(rank, 0 if c.get('has_listings') else 1)][c.get('category') or ''].append(c)

    picked, used_ids, used_categories = [], set(), set()
    for key in sorted(tiers):
        categories = sorted(tiers[key])
        rng.shuffle(categories)
        for category in categories:
            if len(picked) >= k:
                return picked
            if category in used_categories:
                continue
            choice = rng.choice(tiers[key][category])
            picked.append(choice)
            used_ids.add(choice['id'])
            used_categories.add(category)
    # Fewer categories than k: more than one from a category, still best rank first.
    for key in sorted(tiers):
        rest = [c for category in sorted(tiers[key]) for c in tiers[key][category]
                if c['id'] not in used_ids]
        rng.shuffle(rest)
        for c in rest:
            if len(picked) >= k:
                return picked
            picked.append(c)
            used_ids.add(c['id'])
    return picked


def _candidates(account_id: int, now: datetime) -> list:
    """Every channel of the account a check may test: not hidden, and not switched off for
    testing. Two queries however many channels the account has."""
    rows = (db.session.query(Channel.id, Channel.name, Channel.category_name)
            .filter(Channel.account_id == account_id,
                    Channel.hidden.is_(False),
                    Channel.test_enabled.is_(True))
            .all())
    if not rows:
        return []
    listed = {cid for (cid,) in db.session.query(EPGEntry.channel_id)
              .join(Channel, Channel.id == EPGEntry.channel_id)
              .filter(Channel.account_id == account_id,
                      EPGEntry.stop_time > now,
                      EPGEntry.start_time < now + timedelta(hours=LISTINGS_HOURS))
              .distinct()}
    return [{'id': cid, 'name': name, 'category': cat, 'has_listings': cid in listed}
            for cid, name, cat in rows]


# ── Reading the results (pure) ─────────────────────────────────────────────────────────────

def classify(test, signals) -> str:
    """One test's outcome from its ChannelTest row (None when the test never ran) and the
    tester's TestSignals for it. A row that connected played, whatever its quality verdict:
    the question is whether the URL plays, and a low bitrate is an answer to a different one."""
    if test is None:
        return OUT_NOT_RUN
    if test.status == TEST_STATUS_CANCELLED:
        return OUT_INTERRUPTED
    if signals is not None and signals.placeholder:
        return OUT_OFFLINE
    if test.connected:
        return OUT_PLAYED
    stderr = (signals.stderr if signals is not None else '') or ''
    if is_credential_refusal(stderr):
        return OUT_REFUSED
    if is_resolution_failure(stderr):
        return OUT_NO_HOST
    if 'connection refused' in stderr.lower():
        return OUT_UNREACHABLE
    if _NOT_FOUND.search(stderr):
        return OUT_NOT_FOUND
    if _HTTP_ERROR.search(stderr):
        return OUT_HTTP_ERROR
    if (signals is not None and signals.hung) or 'timed out' in stderr.lower():
        return OUT_NO_ANSWER
    return OUT_NO_DATA


def judge(outcomes, sample_size: int) -> Optional[str]:
    """The verdict on the account from its channels' outcomes; None when nothing was actually
    tested, which is a check still owed rather than a result.

    In order: anything that played proves the URLs work; an offline clip proves the server
    took the login; then the account-level failures, each needing two channels to agree (or
    every channel tested, when fewer were); then every channel "not found", which on three or
    more channels points at the URLs' form rather than at the channels. Anything else says
    nothing about the account."""
    counts = Counter(o for o in outcomes if o not in OUT_UNTESTED)
    tested = sum(counts.values())
    if not tested:
        return None
    agree = min(SAME_FAILURE_STOP, tested)
    if counts[OUT_PLAYED]:
        return VERDICT_WORKS
    if counts[OUT_OFFLINE]:
        return VERDICT_LOGIN_OK
    if counts[OUT_REFUSED] >= agree:
        return VERDICT_LOGIN_REFUSED
    if counts[OUT_NO_HOST] + counts[OUT_UNREACHABLE] >= agree:
        return VERDICT_SERVER_UNREACHABLE
    if counts[OUT_NOT_FOUND] == tested and tested >= min(3, sample_size):
        return VERDICT_URLS_NOT_FOUND
    return VERDICT_INCONCLUSIVE


def should_stop(outcomes) -> bool:
    """Whether the sample has its answer: something played, or SAME_FAILURE_STOP channels
    failed the same account-level way."""
    if OUT_PLAYED in outcomes:
        return True
    counts = Counter(outcomes)
    return (counts[OUT_REFUSED] >= SAME_FAILURE_STOP
            or counts[OUT_NO_HOST] + counts[OUT_UNREACHABLE] >= SAME_FAILURE_STOP)


# ── What the page shows (pure) ─────────────────────────────────────────────────────────────

_VERDICT_HEADLINES = {
    VERDICT_WORKS: 'Stream check passed',
    VERDICT_LOGIN_OK: 'Stream check: the provider answered, but no channel played',
    VERDICT_LOGIN_REFUSED: 'Stream check: the provider refused the login',
    VERDICT_SERVER_UNREACHABLE: 'Stream check: the stream server could not be reached',
    VERDICT_URLS_NOT_FOUND: 'Stream check: the provider did not recognize the stream URLs',
    VERDICT_INCONCLUSIVE: 'Stream check: no channel played, but nothing points at the account',
    VERDICT_NO_CHANNELS: 'Stream check: no channels to test',
}


def verdict_explanation(verdict: str, constructed: bool) -> str:
    """What the verdict means and what to do about it, in a sentence or three."""
    fix_urls = ('ChannelBin built these stream URLs itself, so the URL Normalization mode and '
                'the stream server the provider declares are the first two things to check, in '
                'Edit settings. ') if constructed else ''
    if verdict == VERDICT_WORKS:
        return 'A channel played, so this account\'s stream URLs and login work.'
    if verdict == VERDICT_LOGIN_OK:
        return ('The provider accepted the login and answered with its "channel offline" clip, '
                'so the URLs reach it and the channels tried were simply off. Run it again to '
                'try others.')
    if verdict == VERDICT_LOGIN_REFUSED:
        return ('More than one channel was refused with an authorization error, which is the '
                'login, not the channels. Check the username and password in Edit settings, and '
                'that the IPTV subscription is still active.')
    if verdict == VERDICT_SERVER_UNREACHABLE:
        return ('The stream server\'s address did not resolve or did not answer. ' + fix_urls
                + 'If they look right, the provider\'s stream server may be down.').strip()
    if verdict == VERDICT_URLS_NOT_FOUND:
        return ('Every channel tried came back "not found". On a working account that is rare '
                'across this many channels, so the URLs are likely in the wrong form. '
                + fix_urls).strip()
    if verdict == VERDICT_INCONCLUSIVE:
        return ('On most IPTV accounts many channels are off at any given time, so this does not '
                'mean the account is broken: nothing was refused and the server answered. Run it '
                'again to try other channels.')
    if verdict == VERDICT_NO_CHANNELS:
        return 'This account has no channels a check may test (none, or all hidden or switched off for testing).'
    return f'Unknown result "{verdict}".'


def _retry_words(check: AccountStreamCheck) -> str:
    from .tz_utils import format_local
    return format_local(check.retry_at, 'short_datetime') if check.retry_at else 'later'


def summary_line(check: AccountStreamCheck) -> str:
    """The one-line state of a check, for the Content card and the Activity card."""
    if check.status == STREAM_CHECK_PENDING_SYNC:
        return 'Runs after the first sync'
    if check.status == STREAM_CHECK_WAITING:
        return f'Waiting - tries again {_retry_words(check)}'
    if check.status == STREAM_CHECK_RUNNING:
        return f'Testing now, {counted(check.tested_count, "channel")} done'
    if check.status == STREAM_CHECK_DONE:
        if check.verdict == VERDICT_NO_CHANNELS:
            return 'No channels to test'
        return f'{check.played_count} of {counted(check.tested_count, "channel")} played'
    return f'Unknown status {check.status}'


#: How long a passed check's notice stays on the account page. The Content card's row and the
#: Activity card keep the result after that; a green banner forever would be noise.
PASSED_NOTICE_HOURS = 24


def view(check: Optional[AccountStreamCheck], now: Optional[datetime] = None) -> Optional[dict]:
    """What the account page draws for its latest check, or None for an account never checked:
    {'tone', 'title', 'body', 'rows', 'summary', 'summary_tone', 'notice', 'rerun', 'check'}.
    `tone` is a notice class suffix, `rows` the per-channel lines, played first, `notice`
    whether the banner shows and `rerun` whether it offers to run the check again."""
    if check is None:
        return None
    now = now or datetime.utcnow()
    rows = sorted(check.result_list(), key=lambda r: r.get('outcome') != OUT_PLAYED)
    for r in rows:
        r['words'] = OUTCOME_WORDS.get(r.get('outcome'), r.get('outcome') or '')
        r['show_detail'] = bool(r.get('detail')) and r.get('outcome') != OUT_PLAYED
    done = check.status == STREAM_CHECK_DONE
    passed = done and check.verdict == VERDICT_WORKS
    out = {'check': check, 'rows': rows, 'summary': summary_line(check),
           'summary_tone': ('ok' if passed else 'bad' if done and check.verdict in
                            VERDICT_ACCOUNT_PROBLEM else 'warn' if done else 'faint'),
           'notice': not passed or (check.finished_at is not None and check.finished_at
                                    > now - timedelta(hours=PASSED_NOTICE_HOURS)),
           'rerun': done and not passed}
    if check.status == STREAM_CHECK_PENDING_SYNC:
        out.update(tone='info', title='A stream check runs after the first sync',
                   body=f'When the first sync finishes, ChannelBin test-plays up to {SAMPLE_SIZE} '
                        'of this account\'s channels to show its stream URLs work.')
    elif check.status == STREAM_CHECK_WAITING:
        out.update(tone='info', title='Stream check waiting',
                   body=f'{check.wait_reason or "It could not start."} It tries again '
                        f'{_retry_words(check)}.')
    elif check.status == STREAM_CHECK_RUNNING:
        out.update(tone='info', title='Stream check running',
                   body=f'Test-playing up to {SAMPLE_SIZE} channels, one at a time. It stops as '
                        'soon as one plays.')
    elif check.status == STREAM_CHECK_DONE:
        tone = ('ok' if check.verdict == VERDICT_WORKS
                else 'bad' if check.verdict in VERDICT_ACCOUNT_PROBLEM else 'warn')
        out.update(tone=tone, title=_VERDICT_HEADLINES.get(check.verdict, 'Stream check'),
                   body=verdict_explanation(check.verdict, check.constructed))
    else:
        out.update(tone='warn', title=f'Stream check in an unknown state ({check.status})',
                   body='This should be reported.')
    return out


def latest(account_id: int) -> Optional[AccountStreamCheck]:
    return (AccountStreamCheck.query.filter_by(account_id=account_id)
            .order_by(AccountStreamCheck.id.desc()).first())


def result_entry(channel_id, name, category, test, outcome) -> dict:
    """One channel's line in AccountStreamCheck.results. `detail` is the test's own error
    text, already masked by the tester."""
    detail = (test.error_detail or '') if test is not None else ''
    return {'channel_id': channel_id, 'name': name, 'category': category,
            'test_id': test.id if test is not None else None, 'outcome': outcome,
            'detail': detail[:300]}


# ── Running a check ────────────────────────────────────────────────────────────────────────

def test_settings(ct_cfg: dict) -> dict:
    """The health check settings with the check's short test laid over them."""
    from .channel_tester import resolve_health_check_settings
    settings = dict(resolve_health_check_settings(ct_cfg, None))
    settings.update(test_duration_seconds=TEST_SECONDS, connect_retries=CONNECT_RETRIES,
                    connect_retry_delay_seconds=RETRY_DELAY_SECONDS,
                    wait_between_channels_seconds=WAIT_SECONDS)
    return settings


def request_check(app, account_id: int, trigger: str = TRIGGER_MANUAL):
    """Ask for a check now (the account page's menu). Returns (check_id, message).

    One open check per account: asking while one is waiting starts it now, and asking while
    one is running or owed after the first sync says so and starts nothing."""
    @retry_on_locked()
    def _claim():
        account = db.session.get(Account, account_id)
        if account is None:
            return None, None
        open_check = (AccountStreamCheck.query
                      .filter(AccountStreamCheck.account_id == account_id,
                              AccountStreamCheck.status.in_(STREAM_CHECK_OPEN))
                      .order_by(AccountStreamCheck.id.desc()).first())
        if open_check is not None:
            if open_check.status == STREAM_CHECK_PENDING_SYNC and account.status == ACCOUNT_STATUS_OK:
                open_check.status = STREAM_CHECK_WAITING
                open_check.retry_at = datetime.utcnow()
                db.session.commit()
            return open_check.id, open_check.status
        check = AccountStreamCheck(account_id=account_id, trigger=trigger,
                                   status=STREAM_CHECK_WAITING, retry_at=datetime.utcnow(),
                                   wait_reason='Starting.')
        db.session.add(check)
        db.session.commit()
        return check.id, STREAM_CHECK_WAITING

    check_id, status = _claim()
    if check_id is None:
        return None, 'That account no longer exists.'
    if status == STREAM_CHECK_RUNNING:
        return check_id, 'A stream check of this account is already running.'
    if status == STREAM_CHECK_PENDING_SYNC:
        return check_id, ('A stream check of this account is already waiting for its first '
                          'sync. Sync it, and the check runs when the sync finishes.')
    start(app, check_id)
    return check_id, (f'Stream check started: up to {SAMPLE_SIZE} channels, one at a time. '
                      'The result shows on this page.')


def owe_after_first_sync(account_id: int) -> None:
    """Add account's opt-in: a check owed after the first good sync. Adds the row to the
    caller's session without committing, so it lands in the same commit as the account."""
    db.session.add(AccountStreamCheck(account_id=account_id, trigger=TRIGGER_FIRST_SYNC,
                                      status=STREAM_CHECK_PENDING_SYNC))


def after_sync(app, account_id: int) -> None:
    """Called when a sync of the account has ended. A check owed after the first sync starts
    now, if that sync succeeded; one still owed after a failed sync waits for the next."""
    try:
        with app.app_context():
            @retry_on_locked()
            def _release():
                account = db.session.get(Account, account_id)
                if account is None or account.status != ACCOUNT_STATUS_OK:
                    return None
                check = (AccountStreamCheck.query
                         .filter_by(account_id=account_id, status=STREAM_CHECK_PENDING_SYNC)
                         .order_by(AccountStreamCheck.id).first())
                if check is None:
                    return None
                check.status = STREAM_CHECK_WAITING
                check.retry_at = datetime.utcnow()
                check.wait_reason = 'The first sync finished.'
                db.session.commit()
                return check.id

            check_id = _release()
    except Exception:
        log.exception('Stream check: could not release the check owed after the sync of '
                      'account %d', account_id)
        return
    if check_id is not None:
        start(app, check_id)


def start(app, check_id: int) -> None:
    """Run a WAITING check on its own thread now."""
    threading.Thread(target=run_check, args=(app, check_id), daemon=True,
                     name=f'stream-check-{check_id}').start()


def _wait(app, check_id: int, reason: str, minutes: int = RETRY_MINUTES) -> None:
    """Put the check back to WAITING with a retry queued, saying why."""
    from .scheduler import schedule_stream_check
    retry_at = datetime.utcnow() + timedelta(minutes=minutes)
    with app.app_context():
        @retry_on_locked()
        def _commit():
            check = db.session.get(AccountStreamCheck, check_id)
            if check is None:
                return False
            check.status = STREAM_CHECK_WAITING
            check.retry_at = retry_at
            check.wait_reason = reason
            db.session.commit()
            return True

        if not _commit():
            return
        log.info('Stream check %d waiting until %s: %s', check_id, retry_at, reason)
        if not schedule_stream_check(check_id, retry_at):
            log.warning('Stream check %d: no scheduler to queue its retry on', check_id)


def run_check(app, check_id: int) -> None:
    """Run one WAITING check to an answer, or back to WAITING with the reason it could not.
    Thread and scheduler entry point; never raises."""
    try:
        _run_check(app, check_id)
    except Exception:
        log.exception('Stream check %d failed', check_id)
        try:
            _wait(app, check_id, 'The last attempt failed with an error ChannelBin logged.')
        except Exception:
            log.exception('Stream check %d: could not queue a retry after the error', check_id)


def _run_check(app, check_id: int) -> None:
    from .account_blocks import blocked_reason
    from .channel_tester import imminent_recording_conflict, run_channel_sample
    from .config import load_config

    with app.app_context():
        @retry_on_locked()
        def _claim():
            # A conditional UPDATE, so a retry job and a click landing together run it once.
            n = (AccountStreamCheck.query
                 .filter(AccountStreamCheck.id == check_id,
                         AccountStreamCheck.status == STREAM_CHECK_WAITING)
                 .update({'status': STREAM_CHECK_RUNNING, 'started_at': datetime.utcnow(),
                          'wait_reason': None, 'retry_at': None},
                         synchronize_session=False))
            db.session.commit()
            return n == 1

        if not _claim():
            return
        check = db.session.get(AccountStreamCheck, check_id)
        account = db.session.get(Account, check.account_id) if check else None
        if account is None:
            return
        account_id, trigger = account.id, check.trigger
        constructed = bool(account.constructed_stream_url_count)
        ct_cfg = load_config().get('channel_testing', {})

        blocked = blocked_reason(account_id, account.name, capital=True)
        conflict = imminent_recording_conflict() if trigger != TRIGGER_MANUAL else None
        picked = [] if (blocked or conflict) else pick_channels(
            _candidates(account_id, datetime.utcnow()), random.Random())

    if blocked:
        _wait(app, check_id, f'{blocked}.')
        return
    if conflict:
        _wait(app, check_id, conflict)
        return
    if not picked:
        _finish(app, check_id, VERDICT_NO_CHANNELS, [], constructed)
        return

    by_id = {c['id']: c for c in picked}
    entries = []

    def _note(result):
        """Classify one test as it lands and write it to the row, so the page shows the
        check moving; then say whether the sample has its answer."""
        with app.app_context():
            test = (db.session.get(ChannelTest, result['test_id'])
                    if result['test_id'] is not None else None)
            outcome = classify(test, result['signals'])
            c = by_id[result['channel_id']]
            entries.append(result_entry(c['id'], c['name'], c['category'], test, outcome))
            _write_progress(check_id, entries)
        return should_stop([e['outcome'] for e in entries])

    _results, busy = run_channel_sample(app, account_id, [c['id'] for c in picked],
                                        test_settings(ct_cfg), WAIT_SECONDS, stop_after=_note)
    if busy:
        _wait(app, check_id, f'{busy} is using the channel tester.')
        return

    verdict = judge([e['outcome'] for e in entries], len(picked))
    if verdict is None:
        _wait(app, check_id, _untested_reason(entries))
        return
    _finish(app, check_id, verdict, entries, constructed)


def _untested_reason(entries) -> str:
    if any(e['outcome'] == OUT_INTERRUPTED for e in entries):
        return 'A recording needed this account\'s connection, so the check stopped.'
    return ('No channel could be tested: no connection was free on this account (a recording '
            'or another stream was using it).')


@retry_on_locked()
def _write_progress(check_id: int, entries: list) -> None:
    check = db.session.get(AccountStreamCheck, check_id)
    if check is None:
        return
    tested = [e for e in entries if e['outcome'] not in OUT_UNTESTED]
    check.results = json.dumps(entries)
    check.tested_count = len(tested)
    check.played_count = sum(1 for e in tested if e['outcome'] == OUT_PLAYED)
    db.session.commit()


def _finish(app, check_id: int, verdict: str, entries: list, constructed: bool) -> None:
    with app.app_context():
        @retry_on_locked()
        def _commit():
            check = db.session.get(AccountStreamCheck, check_id)
            if check is None:
                return None
            tested = [e for e in entries if e['outcome'] not in OUT_UNTESTED]
            check.status = STREAM_CHECK_DONE
            check.verdict = verdict
            check.finished_at = datetime.utcnow()
            check.constructed = constructed
            check.results = json.dumps(entries)
            check.tested_count = len(tested)
            check.played_count = sum(1 for e in tested if e['outcome'] == OUT_PLAYED)
            db.session.commit()
            account = db.session.get(Account, check.account_id)
            return (check.account_id, account.name if account else f'account {check.account_id}',
                    check.played_count, check.tested_count)

        done = _commit()
        if done is None:
            return
        account_id, name, played, tested = done
        log.info('Stream check %d of %s: %s, %d of %d played', check_id, name, verdict,
                 played, tested)
        _alert(account_id, name, verdict, constructed, entries)


def _alert(account_id: int, name: str, verdict: str, constructed: bool, entries: list) -> None:
    """Raise the standing alert for an account-level verdict; clear it once a check shows the
    account working. An inconclusive check leaves a standing one alone - it is no evidence
    either way."""
    from .accounts import _raise_or_resolve_standing_alert
    source = f'account:{account_id}:stream-check'
    if verdict in VERDICT_ACCOUNT_PROBLEM:
        failed = '; '.join(f'{e["name"]}: {OUTCOME_WORDS.get(e["outcome"], e["outcome"])}'
                           for e in entries if e['outcome'] not in OUT_UNTESTED)
        _raise_or_resolve_standing_alert(
            ALERT_TYPE, source=source, active=True,
            title=f'{name}: {_VERDICT_HEADLINES[verdict].split(": ", 1)[-1]}',
            body=f'{verdict_explanation(verdict, constructed)} Channels tried: {failed}.')
    elif verdict in (VERDICT_WORKS, VERDICT_LOGIN_OK):
        # The type is spelled out, not ALERT_TYPE: the self-clearing audit
        # (tests/test_self_clearing_alerts.py) finds a clearing path by its literal.
        _raise_or_resolve_standing_alert('ACCOUNT_STREAM_CHECK_FAILED', source=source,
                                         active=False)


def recover_at_startup(app) -> None:
    """A check RUNNING at startup was cut off by the restart, and goes back to WAITING; every
    WAITING check gets its retry queued again. Called once from init_scheduler()."""
    from .scheduler import schedule_stream_check
    with app.app_context():
        @retry_on_locked()
        def _reset():
            stranded = AccountStreamCheck.query.filter_by(status=STREAM_CHECK_RUNNING).all()
            for check in stranded:
                check.status = STREAM_CHECK_WAITING
                check.wait_reason = 'ChannelBin restarted while it was running.'
            soon = datetime.utcnow() + timedelta(minutes=1)
            waiting = AccountStreamCheck.query.filter_by(status=STREAM_CHECK_WAITING).all()
            for check in waiting:
                if check.retry_at is None or check.retry_at < soon:
                    check.retry_at = soon
            db.session.commit()
            return [(c.id, c.retry_at) for c in waiting], len(stranded)

        waiting, stranded = _reset()
    if stranded:
        log.warning('Stream check: %d check(s) were running at startup and will run again',
                    stranded)
    for check_id, retry_at in waiting:
        schedule_stream_check(check_id, retry_at)


def delete_for_account(account_id: int) -> None:
    """Teardown for an account delete: the rows (the caller commits) and their retry jobs."""
    from .scheduler import remove_job_if_exists, stream_check_job_id
    for (check_id,) in db.session.query(AccountStreamCheck.id).filter_by(account_id=account_id):
        remove_job_if_exists(stream_check_job_id(check_id))
    AccountStreamCheck.query.filter_by(account_id=account_id).delete(synchronize_session=False)
