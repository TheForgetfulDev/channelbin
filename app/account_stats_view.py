"""What the Accounts pages draw from app/account_stats.py (dev/changelog/1029).

`account_stats` answers "what are the numbers"; this module answers "what does the page
show", so any page that carries the stats makes ONE call - `section_context()` - and
renders the macros in `templates/_account_stats.html`. The window pick, the catch-up
check, the three readers, the pie geometry, the chart columns and every tooltip's wording
live here once, rather than in each route that shows them.

Nothing here reads config or the disk: `cfg` comes in, and every reader is called once per
page for every account together.
"""
import json
import math

from . import account_stats, db
from .database import Account
from .fmt_utils import fmt_duration

WINDOW_PREF_KEY = 'account_stats_window'

WINDOW_LABELS = {'7d': '7 days', '30d': '30 days', '90d': '90 days', 'all': 'All time'}
# How an empty chart finishes the sentence "Nothing was recorded ...".
_WINDOW_PHRASES = {'7d': 'in the last 7 days', '30d': 'in the last 30 days',
                   '90d': 'in the last 90 days', 'all': 'yet'}

# Every number's definition, in one place so the list row, the comparison and the Usage
# card cannot word one number two ways. `&#10;` is the tooltip line break.
TIPS = {
    'capture': ("Recorded time.&#10;Hours of capture from this account's channels: the wall "
                "clock of every segment recorded on one of its channels, added up. A recording "
                "that failed over between accounts credits each account only the part it "
                "captured. Capture time, not the length of the finished video."),
    'recordings': ('Recordings.&#10;Recordings that captured at least one segment on this '
                   'account. A recording that used two accounts counts for both.'),
    'stalls': ("Stalls during capture.&#10;Times a recording on one of this account's channels "
               "stopped receiving data and had to be restarted."),
    'failovers': ("Failed away from.&#10;Times a recording had to leave one of this account's "
                  "channels mid-recording: the feed died, kept stalling, served the provider's "
                  "offline placeholder, or delivered faster than real time."),
    'passed': ("Health checks passed.&#10;Health checks run on this account's channels that "
               "completed. Cancelled checks are not counted either way."),
    'failed': ("Health checks failed.&#10;Health checks run on this account's channels that "
               "failed. Cancelled checks are not counted either way - a cancelled check says "
               "nothing about the channel."),
    'rate': 'Pass rate.&#10;Passed as a share of passed plus failed. Blank when no check ran.',
    'in_group': ('In a channel group.&#10;Channels of this account that belong to at least one '
                 'channel group. Each channel counts once however many groups it is in.'),
    'rec_enabled': ("Recording on.&#10;Group memberships of this account's channels with "
                    "Recording on, i.e. feeds a group may actually record from. Counts "
                    "memberships, so one channel in two groups counts twice."),
    'guide': ("In the TV Guide.&#10;Channels whose listings reach your TV Guide - their own "
              "row, or through a group's row."),
    'epg': ('With EPG.&#10;Channels with an EPG id that has a program showing in the next 24 '
            'hours.'),
    'bands': ("Tested channels by health band.&#10;Of this account's channels that have ever "
              "been scored, how many sit in each band right now. Untested channels are not "
              "shown - no measurement is not a bad measurement."),
    'avg': ("Average health score.&#10;The mean lifetime health score of this account's "
            "tested channels."),
}

# The account page shows Recorded time and Recordings twice - all time on the Content card,
# windowed on the Usage card - so each says which it is, or two different numbers under one
# name on one page would be unexplainable.
ALL_TIME_NOTE = '&#10;&#10;All time, whatever window the Usage card is showing.'
WINDOW_NOTE = '&#10;&#10;Covers the window chosen at the top of this card.'


def rows_tip(total):
    return (f'Guide rows it can feed.&#10;Guide rows this account has a recording-enabled '
            f'member in, plus its own single-channel rows, out of all {total:,} guide rows. '
            f'A fact about reach, not a verdict.')


def failing_tip(cfg):
    from . import health_bands
    from .channel_groups import DEFAULT_FAILING_STREAK_THRESHOLD
    threshold = health_bands.failing_threshold(cfg)
    streak = cfg.get('channel_testing', {}).get('failing_streak_threshold',
                                                DEFAULT_FAILING_STREAK_THRESHOLD)
    rules = []
    if threshold is not None:
        rules.append(f'in the failing band (below {threshold})')
    if streak > 0:
        rules.append(f'on a losing streak of {streak} or more health checks in a row')
    if not rules:
        return ('Failing right now.&#10;Nothing counts as failing: no failing band is set '
                'and the losing-streak rule is off.')
    return ("Failing right now.&#10;Tested channels of this account that currently count as "
            f"failing: {', or '.join(rules)}.")


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

def stored_window():
    """The saved window, or DEFAULT_WINDOW. A stored value that is not a window (hand-edited,
    or from a version with other windows) falls back rather than failing the page."""
    from .database import UserPref
    pref = db.session.get(UserPref, WINDOW_PREF_KEY)
    try:
        value = json.loads(pref.value) if pref and pref.value else None
    except ValueError:
        value = None
    return value if value in account_stats.WINDOWS else account_stats.DEFAULT_WINDOW


def resolve_window(arg):
    """The window a page shows: `?w=` when given, else the saved one. An unknown `?w=` raises
    ValueError - the route answers it with a 400, never a silent fallback."""
    if arg is None or arg == '':
        return stored_window()
    if arg not in account_stats.WINDOWS:
        raise ValueError(f'unknown window {arg!r}')
    return arg


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def fmt_capture(seconds):
    return fmt_duration(seconds) if seconds else '0m'


def fmt_hours(seconds):
    return f'{seconds / 3600:.1f} h' if seconds else '0 h'


def fmt_count(n):
    return f'{int(n or 0):,}'


def fmt_rate(rate):
    return None if rate is None else f'{round(rate * 100)}%'


def _share(value, total):
    if not value:
        return ''
    pct = 100 * value / total
    return '<1%' if pct < 1 else f'{round(pct)}%'


# ---------------------------------------------------------------------------
# Pies
# ---------------------------------------------------------------------------

_PIE_R = 25
_PIE_C = 2 * math.pi * _PIE_R
# The surface-colored gap between two slices, in circumference units of the 100-unit box.
_PIE_GAP = 0.9


def pie(title, scope, accounts, values, fmt, empty):
    """One part-of-the-whole card: SVG arcs in account colors plus the legend that labels
    every slice directly (name, number, share), so color is never the only encoding."""
    total = sum(values.get(a.id, 0) for a in accounts)
    parts = [{'name': a.name, 'color': a.color, 'value': values.get(a.id, 0),
              'text': fmt(values.get(a.id, 0)), 'share': _share(values.get(a.id, 0), total)}
             for a in accounts]
    arcs = []
    if total:
        live = [p for p in parts if p['value']]
        gap = _PIE_GAP if len(live) > 1 else 0
        offset = 0.0
        for p in live:
            length = p['value'] / total * _PIE_C
            arcs.append({'color': p['color'], 'dash': f'{max(0.1, length - gap):.2f} {_PIE_C:.2f}',
                         'offset': f'{-offset:.2f}',
                         'tip': f"{p['name']}&#10;{title}: {p['text']} ({p['share']} of {fmt(total)})"})
            offset += length
    return {'title': title, 'scope': scope, 'parts': parts, 'arcs': arcs, 'total': total,
            'total_text': fmt(total), 'empty': empty}


# ---------------------------------------------------------------------------
# Trend columns
# ---------------------------------------------------------------------------

def _bucket_labels(unit, start):
    """(axis label, tooltip title) for a bucket starting on `start`."""
    if unit == 'day':
        short = f'{start:%b} {start.day}'
        return short, short
    if unit == 'week':
        short = f'{start:%b} {start.day}'
        return short, f'Week of {short}'
    if unit == 'month':
        return f'{start:%b}', f'{start:%b} {start.year}'
    if unit == 'quarter':
        q = (start.month - 1) // 3 + 1
        return f'Q{q} {start.year % 100:02d}', f'Q{q} {start.year}'
    if unit == 'year':
        return str(start.year), str(start.year)
    raise ValueError(f'unknown trend unit {unit!r}')


# At most this many axis labels; past it every Nth is shown (and always the last).
_AXIS_LABELS = 8


def columns(trend, layers, fmt, empty):
    """A stacked column chart as plain data for the template.

    `layers` stack bottom-up: [{'name', 'color' or 'cls', 'values': [per bucket],
    'detail': [per bucket] or None}]. Heights are percentages of the tallest column. A
    bucket that measured nothing is drawn as a stub (`zero`), so it reads as "nothing", not
    as a gap. Each column's breakdown rides in `tips` for the page's HTML tooltip - there
    is no legend under the chart."""
    buckets = trend['buckets']
    totals = [sum(layer['values'][i] for layer in layers) for i in range(len(buckets))]
    peak = max(totals, default=0)
    if not peak:
        return {'empty': empty}
    n = len(buckets)
    step = 1 if n <= _AXIS_LABELS else math.ceil(n / _AXIS_LABELS)
    cols, axis, tips = [], [], []
    for i, bucket in enumerate(buckets):
        short, title = _bucket_labels(trend['unit'], bucket['start'])
        segs = [{'cls': layer.get('cls'), 'color': layer.get('color'),
                 'pct': f"{100 * layer['values'][i] / peak:.2f}"}
                for layer in layers if layer['values'][i]]
        cols.append({'segs': segs, 'zero': not totals[i]})
        axis.append(short if (i % step == 0 or i == n - 1) else '')
        rows = [{'color': layer.get('color') or f"var(--{layer['cls']})", 'name': layer['name'],
                 'value': fmt(layer['values'][i]),
                 'detail': layer['detail'][i] if layer.get('detail') else ''}
                for layer in layers if layer['values'][i]]
        tips.append({'title': title, 'rows': rows,
                     'total': fmt(totals[i]) if len(rows) > 1 else None})
    return {'cols': cols, 'axis': axis, 'peak': fmt(peak), 'tips': json.dumps(tips),
            'unit': trend['unit']}


def _series(trend, account_id, column):
    return [b['values'][account_id][column] for b in trend['buckets']]


# ---------------------------------------------------------------------------
# The one call a page makes
# ---------------------------------------------------------------------------

def section_context(window, cfg, app, account_ids=None):
    """Everything the stats macros render, for every account, oldest first - the order the
    Accounts list shows them in, and `ctx['accounts']` is the list a page should render.
    `account_ids` narrows it: the account page passes its own id and gets the one-account
    rendering, the same Usage card a one-account /accounts draws.

    `window` must already be resolved (`resolve_window`). Runs the catch-up check, then
    each reader once for all the accounts together. `current` is also what the list rows'
    second line reads, so a page that shows both pays for current_stats once.

    **Call it before the page loads anything else from the session.** The catch-up fold
    commits, and a commit expires every row already loaded, so rows fetched earlier would
    each be re-read one query at a time when the template touches them - an N+1 that
    tests/test_scaling_pages.py caught on /accounts. That is also why this loads the
    accounts itself rather than taking them."""
    notice = account_stats.ensure_fresh(app)
    query = Account.query
    if account_ids is not None:
        query = query.filter(Account.id.in_(account_ids))
    accounts = query.order_by(Account.created_at).all()
    ids = [a.id for a in accounts]
    today = account_stats.today_local()
    windowed = account_stats.windowed_stats(ids, window, today)
    trend = account_stats.trend(ids, window, today)
    current = account_stats.current_stats(ids, cfg)
    phrase = _WINDOW_PHRASES[window]
    rows_total = next(iter(current.values()))['guide_rows_total'] if current else 0

    ctx = {
        'window': window,
        'windows': [(key, WINDOW_LABELS[key]) for key in account_stats.WINDOWS],
        'window_label': WINDOW_LABELS[window],
        'notice': notice,
        'accounts': accounts,
        'windowed': windowed,
        'current': current,
        'single': len(accounts) == 1,
        'tips': TIPS,
        'window_note': WINDOW_NOTE,
        'rows_tip': rows_tip(rows_total),
        'failing_tip': failing_tip(cfg),
    }
    if not accounts:
        return ctx

    if len(accounts) == 1:
        a = accounts[0]
        checks = columns(trend, [
            {'name': 'Passed', 'cls': 'ok', 'values': _series(trend, a.id, 'checks_passed')},
            {'name': 'Failed', 'cls': 'bad', 'values': _series(trend, a.id, 'checks_failed')},
        ], fmt_count, _checks_empty_single(a.id, window, current, today, phrase))
        ctx['charts'] = {
            'capture': columns(trend, [{'name': a.name, 'color': 'var(--accent)',
                                        'values': _series(trend, a.id, 'capture_seconds')}],
                               fmt_hours, f'Nothing was recorded {phrase}.'),
            'checks': checks,
        }
        return ctx

    totals = {c: sum(windowed[a.id][c] for a in accounts)
              for c in ('capture_seconds', 'recordings', 'stalls', 'failovers_away',
                        'checks_passed', 'checks_failed')}
    checked = totals['checks_passed'] + totals['checks_failed']
    totals['pass_rate'] = totals['checks_passed'] / checked if checked else None
    ctx['totals'] = totals

    scope = 'all time' if window == 'all' else f'last {WINDOW_LABELS[window]}'
    ctx['pies'] = [
        pie('Recorded time', scope, accounts,
            {a.id: windowed[a.id]['capture_seconds'] for a in accounts}, fmt_capture,
            f'Nothing was recorded {phrase}.'),
        pie('Health checks passed', scope, accounts,
            {a.id: windowed[a.id]['checks_passed'] for a in accounts}, fmt_count,
            f'No health check passed {phrase}.'),
        pie('In the TV Guide', 'now', accounts,
            {a.id: current[a.id]['guide_channels'] for a in accounts}, fmt_count,
            'No channel is in the TV Guide yet.'),
        pie('In a channel group', 'now', accounts,
            {a.id: current[a.id]['in_group'] for a in accounts}, fmt_count,
            'No channel is in a group yet.'),
    ]

    def checks_detail(aid):
        passed = _series(trend, aid, 'checks_passed')
        failed = _series(trend, aid, 'checks_failed')
        return [f'{p:,} passed, {f:,} failed' for p, f in zip(passed, failed)]

    ctx['charts'] = {
        'capture': columns(trend, [{'name': a.name, 'color': a.color,
                                    'values': _series(trend, a.id, 'capture_seconds')}
                                   for a in accounts],
                           fmt_hours, f'Nothing was recorded {phrase}.'),
        'checks': columns(trend, [{'name': a.name, 'color': a.color,
                                   'values': [p + f for p, f in zip(
                                       _series(trend, a.id, 'checks_passed'),
                                       _series(trend, a.id, 'checks_failed'))],
                                   'detail': checks_detail(a.id)}
                                  for a in accounts],
                          fmt_count, f'No health checks {phrase}.'),
    }
    return ctx


def _checks_empty_single(account_id, window, current, today, phrase):
    """A never-tested account says so, rather than claiming a quiet week."""
    if current[account_id]['tested']:
        return f'No health checks {phrase}.'
    if window != 'all':
        ever = account_stats.windowed_stats([account_id], 'all', today)[account_id]
        if ever['checks_passed'] or ever['checks_failed']:
            return f'No health checks {phrase}.'
    return 'Never health checked.'

