"""The EPG source scorecard: the numbers that say which of an account's guides to keep
(DESIGN-epg-sources.md §12.13, dev/changelog/1717).

Read from what the database already holds, never fetched: the source directory (coverage,
one title all day, reach, the detail counts the import's scan pass makes), the key answers,
the refresh history and the stored agreement of two sources per channel. The numbers a page
load cannot afford are stored by the import or by the check below; everything else is
computed per request in a fixed number of queries whatever the channel count.

Agreement is compare_listings()'s verdict, the one judge of "the same program", over the
listings both sources hold from now on. It costs about 3 ms a channel and pair (27 s for
account 5's three sources on prod), so it runs in the background after an import
(run_check(), admission kind KIND_EPG_CHECK) and is stored in epg_source_agreements.

The same check stores each account's findings (dev/changelog/1718): name matches waiting,
an id unrelated channels share, a source serving channels better than their own, a source
adding nothing. Shown on pages, never raised as alerts. Then which followed sports games
each source would match, by the sports matcher's own judge (dev/changelog/1726).

Nothing here writes a channel's guide. "Best here" is a suggestion with its reasons; acting
on it is the user's, through set_source_override()."""
from __future__ import annotations

import json
import logging
import time as time_mod
from collections import Counter
from datetime import date, datetime
from typing import NamedTuple

from sqlalchemy import delete, select

from . import admission, db
from .database import (Account, Channel, EPGEntry, EpgAlternateEntry, EpgFinding, EpgSource,
                       EpgSourceAgreement, EpgSourceGames, EpgSourceChannel, EpgSourceRefresh,
                       EpgSourceSubscription, EPG_FINDING_ADDS_NOTHING,
                       EPG_FINDING_BETTER_SOURCE, EPG_FINDING_NAME_MATCHES,
                       EPG_FINDING_SHARED_ID, EPG_STATUS_FAILED, EPG_STATUS_OK,
                       EPG_STATUS_REFUSED, EPG_STATUS_TRUNCATED)
from .db_utils import chunked, retry_on_locked
from .epg_sources import (CMP_AGREEING, CMP_NO_OVERLAP, CMP_UNRELATED, MATCH_DISAGREE, MATCH_NEW,
                          REAL_SCHEDULE_MIN_TITLES, Listing,
                          Reach, account_filling_sources, channel_key, compare_listings, file_id_for, key_answers,
                          norm_key, of_sources, placeholder_names, reach_from_horizons,
                          sources_without_directory)
from .fmt_utils import counted, iso_or_none, plural

log = logging.getLogger(__name__)

#: Verdicts that say two sources list the same channel's schedule (§14.3's "agree").
AGREEING = CMP_AGREEING

#: A difference in the share of described listings smaller than this is not a reason to
#: prefer one source (percentage points).
DETAIL_MARGIN = 10

_CHUNK = 500
RETRY_MINUTES = 10


# ── What the directory says about one channel ────────────────────────────────

class ChannelSource(NamedTuple):
    """One source's directory row for one channel: what that channel reads from it."""
    source_id: int
    programs: int
    distinct_titles: int
    horizon_until: datetime | None
    described: int | None
    subtitled: int | None
    categorized: int | None
    rated: int | None
    flagged: int | None
    doubled: int | None

    @property
    def real_schedule(self) -> bool:
        return self.distinct_titles >= REAL_SCHEDULE_MIN_TITLES

    def share(self, field: str) -> int | None:
        n = getattr(self, field)
        if n is None or not self.programs:
            return None
        return round(n * 100 / self.programs)


class Reliability(NamedTuple):
    """A source's kept refreshes (EPG_REFRESH_HISTORY_LIMIT at most), by how they ended."""
    refreshes: int
    ok: int
    failed: int
    refused: int
    truncated: int
    since: datetime | None          # the oldest kept refresh
    last_trouble: datetime | None   # the latest one that did not end OK
    last_trouble_status: str | None


class SourceScore(NamedTuple):
    source_id: int
    name: str
    priority: int
    has_directory: bool
    covered: int            # visible channels whose key reads listings here
    only_here: int          # ... that no other source the account reads covers
    guides: int             # channels whose guide comes from it now
    real_schedule: int
    one_title: int
    programs: int           # in-window listings across the covered channels
    counted: int            # ... of which the detail counts exist for (NULL rows left out)
    described: int
    subtitled: int
    categorized: int
    rated: int
    flagged: int
    doubled_channels: int   # covered channels listing two programs at one moment
    doubled_unknown: int    # covered channels whose doubles were not counted
    reach: Reach | None     # over the covered channels' own rows
    compared: int           # covered channels judged against at least one other source
    agree: int              # ... agreeing with every source it was judged against
    disagree: int           # ... unrelated to at least one
    outvoted: int           # ... unrelated to all, where the others agree with each other
    reliability: Reliability


class AccountScorecard(NamedTuple):
    account_id: int
    sources: list           # [SourceScore], in the account's order
    guided: Reach | None    # the account's guided channels, each at its own guide's last day
    channels: list          # [ChannelScore] for channels two or more sources cover


class ChannelScore(NamedTuple):
    channel_id: int
    name: str | None
    current: int | None     # Channel.epg_source_id
    best: int
    reasons: list           # [str]
    sources: list           # [ChannelSource], in the account's order
    verdicts: dict          # {(earlier, later): CMP_*}
    pinned: bool            # the user chose this channel's source (epg_source_override_id)
    why: str | None         # the RULE_* that set `current` aside, when another source is best
    filled: tuple = ()      # the sources continuing its guide past `current`'s end, in order
    # Its guide, continued, runs through the day `best` does: reach is no reason to change.
    reach_met: bool = False


#: The rules of best_source(), in the order it applies them: the one that set a source aside.
RULE_OUTVOTED = 'outvoted'
RULE_ONE_TITLE = 'one_title'
RULE_DOUBLED = 'doubled'
RULE_REACH = 'reach'
RULE_DETAIL = 'detail'
RULE_ORDER = 'order'


# ── The one batch reader ─────────────────────────────────────────────────────

_DIR = EpgSourceChannel.__table__
_ROW_COLUMNS = (_DIR.c.source_id, _DIR.c.xml_id, _DIR.c.entry_count, _DIR.c.distinct_titles,
                _DIR.c.horizon_until, _DIR.c.described, _DIR.c.subtitled, _DIR.c.categorized,
                _DIR.c.rated, _DIR.c.flagged, _DIR.c.doubled)


def _directories(source_ids, case_sensitive: bool):
    """({source_id: {folded key: {xml_id: entry_count}}}, {(source_id, xml_id): row}) over
    directory rows with listings: the variants file_id_for() chooses among, so a channel is
    scored on exactly the id the import reads for it. One query.

    Read through the table, not the mapped class: the ORM's row loading was two thirds of
    a 0.6 s Sources card over epg-full's 43,000 rows, of which account 5 uses 3,000."""
    variants: dict[int, dict[str, dict[str, int]]] = {sid: {} for sid in source_ids}
    rows: dict[tuple[int, str], tuple] = {}
    if not variants:
        return variants, rows
    for r in db.session.execute(select(*_ROW_COLUMNS).where(
            _DIR.c.source_id.in_(list(variants)), _DIR.c.entry_count > 0)):
        sid, xml_id, n = r[0], r[1], r[2]
        variants[sid].setdefault(norm_key(xml_id, case_sensitive), {})[xml_id] = n
        rows[(sid, xml_id)] = r
    return variants, rows


def channel_sources(channels, order, answers, variants, rows,
                    case_sensitive: bool) -> dict[int, list[ChannelSource]]:
    """{channel_id: [ChannelSource]} in `order` for each channel some source covers - pure.
    `channels` is (id, epg_channel_id) pairs; `answers` key_answers() over `order`."""
    out: dict[int, list[ChannelSource]] = {}
    for cid, provider_id in channels:
        found = []
        for sid in order:
            key = channel_key(provider_id, answers.get((cid, sid)))
            if not key:
                continue
            fid = file_id_for(key, variants.get(sid, {}).get(norm_key(key, case_sensitive), {}))
            if fid is None:
                continue
            r = rows[(sid, fid)]
            found.append(ChannelSource(sid, r[2], r[3] or 0, r[4], *r[5:]))
        if found:
            out[cid] = found
    return out


def reliability(history) -> Reliability:
    """Reliability from [(finished_at, status), ...], newest first - pure."""
    by = Counter(status for _at, status in history)
    trouble = next(((at, status) for at, status in history if status != EPG_STATUS_OK),
                   (None, None))
    return Reliability(len(history), by[EPG_STATUS_OK], by[EPG_STATUS_FAILED],
                       by[EPG_STATUS_REFUSED], by[EPG_STATUS_TRUNCATED],
                       history[-1][0] if history else None, *trouble)


def _refresh_history(source_ids) -> dict[int, list]:
    out: dict[int, list] = {sid: [] for sid in source_ids}
    if out:
        for sid, at, status in (db.session.query(EpgSourceRefresh.source_id,
                                                 EpgSourceRefresh.finished_at,
                                                 EpgSourceRefresh.status)
                                .filter(EpgSourceRefresh.source_id.in_(list(out)))
                                .order_by(EpgSourceRefresh.finished_at.desc(),
                                          EpgSourceRefresh.id.desc())):
            out[sid].append((at, status))
    return out


def _stored_verdicts(account_id: int, order) -> dict[int, dict[tuple[int, int], str]]:
    """{channel_id: {(earlier, later): verdict}} for this account's channels, keeping only
    pairs of sources it reads now, in its order now. One query."""
    rank = {sid: i for i, sid in enumerate(order)}
    out: dict[int, dict] = {}
    rows = (db.session.query(EpgSourceAgreement.channel_id, EpgSourceAgreement.source_id,
                             EpgSourceAgreement.other_source_id, EpgSourceAgreement.verdict)
            .join(Channel, Channel.id == EpgSourceAgreement.channel_id)
            .filter(Channel.account_id == account_id))
    for cid, a, b, verdict in rows:
        if a in rank and b in rank:
            pair = (a, b) if rank[a] < rank[b] else (b, a)
            out.setdefault(cid, {})[pair] = verdict
    return out


def outvoted(verdicts: dict, source_ids) -> set[int]:
    """The sources unrelated to every other source judged against them on a channel, where
    at least two of those others agree with each other - pure. Two sources disagreeing
    with each other is not a vote: neither is left out."""
    def v(a, b):
        return verdicts.get((a, b)) or verdicts.get((b, a))

    out = set()
    for s in source_ids:
        judged = [o for o in source_ids if o != s and v(s, o) not in (None, CMP_NO_OVERLAP)]
        if len(judged) < 2 or any(v(s, o) != CMP_UNRELATED for o in judged):
            continue
        if any(v(a, b) in AGREEING for i, a in enumerate(judged) for b in judged[i + 1:]):
            out.add(s)
    return out


class Choice(NamedTuple):
    best: int
    reasons: list           # [str], in the order the rules applied
    dropped: dict           # {source_id: RULE_*} - the rule that set each other source aside


def best_source(sources: list, verdicts: dict, names: dict, day_of) -> tuple[int, list]:
    """(source_id, [reason, ...]) - choose_source() without the rule per source."""
    choice = choose_source(sources, verdicts, names, day_of)
    return choice.best, choice.reasons


def choose_source(sources: list, verdicts: dict, names: dict, day_of) -> Choice:
    """Which of a channel's sources serves it best, why, and which rule set each other source
    aside, in the order the rules are applied. Pure. `sources` is [ChannelSource] in the account's
    order; `day_of(horizon)` the local day a horizon runs through, as display text.

    1. A source the others outvote (outvoted()) is left out.
    2. A full schedule beats one program all day.
    3. One that never lists two programs at a moment beats one that does, when every source's
       doubles were counted.
    4. The longest reach, by whole days.
    5. More described listings, by DETAIL_MARGIN points or more.
    6. Otherwise the first in the account's order.
    Reach comes before detail because a rule or Record cannot schedule past the guide's end;
    detail is a better listing of a program the guide already has."""
    left = list(sources)
    reasons = []
    set_aside: dict[int, str] = {}

    def narrow(keep, why, rule):
        nonlocal left
        kept = [s for s in left if keep(s)]
        dropped = [s for s in left if not keep(s)]
        if kept and dropped:
            reasons.append(why(kept, dropped))
            set_aside.update((s.source_id, rule) for s in dropped)
            left = kept

    def named(group):
        return ', '.join(names.get(s.source_id, f'source {s.source_id}') for s in group)

    gone = outvoted(verdicts, [s.source_id for s in left])
    narrow(lambda s: s.source_id not in gone,
           lambda k, d: f'{named(d)} does not match the other sources, which match each other',
           RULE_OUTVOTED)
    narrow(lambda s: s.real_schedule,
           lambda k, d: f'{named(d)} lists one program all day', RULE_ONE_TITLE)
    # Only when every source was counted: None is "not counted", never "never doubles".
    if all(s.doubled is not None for s in left):
        narrow(lambda s: not s.doubled,
               lambda k, d: f'{named(d)} lists two programs at the same time', RULE_DOUBLED)
    days = {s.source_id: day_of(s.horizon_until) for s in left if s.horizon_until}
    if days:
        last = max(s.horizon_until for s in left if s.horizon_until)
        top = day_of(last)
        narrow(lambda s: s.horizon_until is not None and day_of(s.horizon_until) == top,
               lambda k, d: f'{named(k)} runs to {top}, {named(d)} stops sooner', RULE_REACH)
    shares = {s.source_id: s.share('described') for s in left}
    if all(v is not None for v in shares.values()) and shares:
        most = max(shares.values())
        narrow(lambda s: shares[s.source_id] >= most - DETAIL_MARGIN,
               lambda k, d: (f'{named(k)} describes {shares[k[0].source_id]}% of its listings, '
                             f'{named(d)} {shares[d[0].source_id]}%'), RULE_DETAIL)
    if len(left) > 1:
        reasons.append(f'{named(left[:1])} is first in this account\'s order')
        set_aside.update((s.source_id, RULE_ORDER) for s in left[1:])
    return Choice(left[0].source_id, reasons, set_aside)


def account_scorecard(account_id: int, tz, case_sensitive: bool,
                      day_of=None, with_channels: bool = False) -> AccountScorecard:
    """The scorecard for one account. Eight queries whatever the channel or source count
    (subscriptions, sources, channels, key answers, directories, refresh history and, with
    more than one source, the stored agreements and the sources continuing each guide). `day_of(horizon)` is
    the display text of a horizon's last local day, for the per-channel reasons."""
    subs = (db.session.query(EpgSourceSubscription.source_id, EpgSourceSubscription.priority)
            .filter(EpgSourceSubscription.account_id == account_id)
            .order_by(EpgSourceSubscription.priority).all())
    order = [sid for sid, _p in subs]
    if not order:
        return AccountScorecard(account_id, [], None, [])
    names = dict(db.session.query(EpgSource.id, EpgSource.name)
                 .filter(EpgSource.id.in_(order)))
    ch = Channel.__table__.c
    channels = db.session.execute(
        select(ch.id, ch.epg_channel_id, ch.epg_source_id, ch.name, ch.epg_source_override_id)
        .where(ch.account_id == account_id, ch.hidden.is_(False))).all()
    answers = key_answers(order)
    variants, rows = _directories(order, case_sensitive)
    per_channel = channel_sources([(c[0], c[1]) for c in channels], order, answers, variants,
                                  rows, case_sensitive)
    history = _refresh_history(order)
    verdicts = _stored_verdicts(account_id, order) if len(order) > 1 else {}
    no_directory = sources_without_directory(order)

    # A guide a later source continues runs as far as the furthest of its sources
    # (epg_sources.place_stretches()).
    filled = account_filling_sources(account_id) if len(order) > 1 else {}
    guides = Counter(c[2] for c in channels if c[2] is not None)
    # Horizons are counted by value: channels share a few hundred of them, and turning each
    # into a local day is what costs (0.5 s per call over account 2's 29,000 channels).
    guided_horizons = Counter()
    guide_end: dict[int, datetime] = {}
    for cid, _pid, current, _name, _pin in channels:
        mine = {current, *filled.get(cid, ())}
        ends = [cs.horizon_until for cs in per_channel.get(cid, ())
                if cs.source_id in mine and cs.horizon_until is not None]
        if ends and current is not None:
            guide_end[cid] = max(ends)
            guided_horizons[guide_end[cid]] += 1
    scores = []
    for sid, priority in subs:
        tally = Counter()
        horizons = Counter()
        for cid, found in per_channel.items():
            cs = next((x for x in found if x.source_id == sid), None)
            if cs is None:
                continue
            tally['covered'] += 1
            if len(found) == 1:
                tally['only_here'] += 1
            if cs.real_schedule:
                tally['real'] += 1
            elif cs.distinct_titles == 1:
                tally['one'] += 1
            tally['programs'] += cs.programs
            if cs.described is not None:
                tally['counted'] += cs.programs
                for f in ('described', 'subtitled', 'categorized', 'rated', 'flagged'):
                    tally[f] += getattr(cs, f) or 0
            if cs.doubled is None:
                tally['doubled_unknown'] += 1
            elif cs.doubled:
                tally['doubled'] += 1
            if cs.horizon_until is not None:
                horizons[cs.horizon_until] += 1
            mine = {pair: v for pair, v in verdicts.get(cid, {}).items()
                    if sid in pair and v != CMP_NO_OVERLAP}
            if mine:
                tally['compared'] += 1
                if all(v in AGREEING for v in mine.values()):
                    tally['agree'] += 1
                else:
                    tally['disagree'] += 1
                    if sid in outvoted(verdicts[cid], [x.source_id for x in found]):
                        tally['outvoted'] += 1
        scores.append(SourceScore(
            sid, names.get(sid, f'source {sid}'), priority, sid not in no_directory,
            tally['covered'], tally['only_here'], guides.get(sid, 0), tally['real'],
            tally['one'], tally['programs'], tally['counted'], tally['described'],
            tally['subtitled'], tally['categorized'], tally['rated'], tally['flagged'],
            tally['doubled'], tally['doubled_unknown'], reach_from_horizons(horizons.items(), tz),
            tally['compared'], tally['agree'], tally['disagree'], tally['outvoted'],
            reliability(history.get(sid, []))))

    channel_scores = []
    if with_channels:
        by_id = {c[0]: c for c in channels}
        day_of = day_of or (lambda h: h.date().isoformat())
        for cid, found in per_channel.items():
            if len(found) < 2:
                continue
            v = verdicts.get(cid, {})
            choice = choose_source(found, v, names, day_of)
            current = by_id[cid][2]
            best_end = next((cs.horizon_until for cs in found
                             if cs.source_id == choice.best), None)
            channel_scores.append(ChannelScore(
                cid, by_id[cid][3], current, choice.best, choice.reasons, found, v,
                by_id[cid][4] is not None, choice.dropped.get(current),
                tuple(filled.get(cid, ())),
                bool(best_end is not None and cid in guide_end
                     and (guide_end[cid] >= best_end
                          or day_of(guide_end[cid]) == day_of(best_end)))))
        channel_scores.sort(key=lambda c: ((c.name or '').lower(), c.channel_id))
    return AccountScorecard(account_id, scores, reach_from_horizons(guided_horizons.items(), tz),
                            channel_scores)


def name_match_counts(account_id: int, source_ids, case_sensitive: bool) -> dict[int, dict]:
    """{source_id: {'new': n, 'disagree': n}} - this account's channels the review page
    would propose a name match for: channels with no listings there today that a name
    would give some, and channels whose key and name point at different rows. Read
    through name_match_review(), so it is the review page's own count; a full read of each
    directory, so the API pays it and the Sources card does not."""
    from .epg_sources import MATCH_DISAGREE, MATCH_NEW, name_match_review
    out = {}
    for source in EpgSource.query.filter(EpgSource.id.in_(list(source_ids))):
        kinds = Counter(p.kind for p in name_match_review(source, case_sensitive).proposals
                        if p.channel.account_id == account_id)
        out[source.id] = {'new': kinds[MATCH_NEW], 'disagree': kinds[MATCH_DISAGREE]}
    return out


# ── What the Sources card and the API show ───────────────────────────────────

def _pct(n: int, total: int) -> int | None:
    return round(n * 100 / total) if total else None


def card_line(score: SourceScore) -> str | None:
    """The Sources card's one line for a source: 'Covers 3,123 channels here, 461 only here.
    437 list one program all day.' None for a source with no directory."""
    if not score.has_directory:
        return None
    if not score.covered:
        return "Covers none of this account's channels."
    text = f"Covers {counted(score.covered, 'channel')} here"
    text += f', {score.only_here:,} only here.' if score.only_here else ', all also in another source.'
    if score.one_title:
        text += (f' {score.one_title:,} {plural(score.one_title, "lists", "list")} one program '
                 'all day.')
    if score.disagree:
        text += (f' {score.disagree:,} {plural(score.disagree, "does", "do")} not match another '
                 'source.')
    return text


def reliability_words(r: Reliability) -> str:
    """The tooltip line for a source's refreshes."""
    if not r.refreshes:
        return 'No refreshes recorded yet.'
    text = f'{r.ok:,} of the last {counted(r.refreshes, "refresh")} succeeded'
    trouble = [(r.failed, 'failed to download'), (r.refused, 'refused as too small'),
               (r.truncated, 'stopped partway')]
    parts = [f'{n:,} {what}' for n, what in trouble if n]
    return text + (f' ({", ".join(parts)}).' if parts else '.')


def card_tip(score: SourceScore) -> str:
    """The tooltip under the card line: detail shares, doubled listings, reliability."""
    lines = []
    if score.counted:
        shares = [(score.described, 'described'), (score.subtitled, 'with a subtitle'),
                  (score.categorized, 'with a category'), (score.rated, 'with a rating'),
                  (score.flagged, 'marked new or live')]
        lines.append(f'Of {counted(score.counted, "listing")} for this account\'s channels: '
                     + ', '.join(f'{_pct(n, score.counted)}% {what}' for n, what in shares) + '.')
    elif score.covered:
        lines.append('How much detail its listings carry is counted at its next refresh.')
    if score.doubled_channels:
        lines.append(f'{counted(score.doubled_channels, "channel")} '
                     f'{plural(score.doubled_channels, "lists", "list")} two programs at the '
                     'same time.')
    if score.doubled_unknown:
        lines.append(f'Two programs at once is not counted yet on '
                     f'{counted(score.doubled_unknown, "channel")}: it is counted at the '
                     'next refresh.')
    if score.compared:
        lines.append(f'Compared with the other sources on {counted(score.compared, "channel")}: '
                     f'{score.agree:,} match, {score.disagree:,} do not'
                     + (f' ({score.outvoted:,} where the others match each other)'
                        if score.outvoted else '') + '.')
    lines.append(reliability_words(score.reliability))
    return '\n'.join(lines)


def _reach_json(reach: Reach | None):
    if reach is None:
        return None
    return {'day': reach.day.isoformat(), 'latest': reach.latest.isoformat(),
            'channels': reach.channels,
            'days': [[d.isoformat(), n] for d, n in reach.days]}


def scorecard_json(card: AccountScorecard, names: dict | None = None) -> dict:
    """The API's body: every number, unformatted, for the EPG Manager to lay out. `names`
    is name_match_counts()."""
    sources = []
    for s in card.sources:
        d = s._asdict()
        d['name_matches'] = (names or {}).get(s.source_id)
        d['reach'] = _reach_json(s.reach)
        d['reliability'] = {**s.reliability._asdict(), 'since': iso_or_none(s.reliability.since),
                            'last_trouble': iso_or_none(s.reliability.last_trouble)}
        sources.append(d)
    channels = [{'channel_id': c.channel_id, 'name': c.name, 'current': c.current,
                 'best': c.best, 'reasons': c.reasons, 'filled': list(c.filled),
                 'sources': [{**cs._asdict(), 'horizon_until': iso_or_none(cs.horizon_until),
                              'described_pct': cs.share('described')} for cs in c.sources],
                 'verdicts': [{'source_id': a, 'other_source_id': b, 'verdict': v}
                              for (a, b), v in c.verdicts.items()]}
                for c in card.channels]
    return {'account_id': card.account_id, 'sources': sources,
            'guided': _reach_json(card.guided), 'channels': channels}


def card_json(card: AccountScorecard, label, today: date) -> dict:
    """What the account page fills in after it draws: {'sources': [{'source_id', 'line',
    'tip'}], 'guide_days': text or None}. `label(day)` is a day's display text."""
    return {'sources': [{'source_id': s.source_id, 'line': card_line(s), 'tip': card_tip(s)}
                        for s in card.sources if card_line(s)],
            'guide_days': days_breakdown(card.guided, today, label)}


def days_breakdown(reach: Reach | None, today: date, label) -> str | None:
    """The account's guided channels by the last day their guide runs through, for the
    Content card's reach tooltip: 'Fri Nov 28: 2,341 (70%)'. `label(day)` is the day's
    display text."""
    if reach is None or not reach.days:
        return None
    lines = [f'{counted(reach.channels, "channel")} with a guide here, by the last day it runs '
             'through:']
    gone = 0
    for day, n in reach.days:
        if day < today:
            gone += n
            continue
        lines.append(f'{label(day)}: {n:,} ({_pct(n, reach.channels)}%)')
    if gone:
        lines.append(f'Already ran out: {gone:,} ({_pct(gone, reach.channels)}%)')
    return '\n'.join(lines)


# ── Findings: what an account could improve (dev/changelog/1718) ─────────────
#
# Found by the check below from what the database holds, stored per account, shown on the
# Sources card and (the two a person should act on) the Dashboard, and never an alert. A
# finding only reads; acting on it is a person pressing a button that calls the writer the
# line names (DESIGN-epg-sources.md §16).

class Finding(NamedTuple):
    source_id: int
    kind: str               # EPG_FINDING_*
    subject: str | None     # the shared id
    count: int
    detail: dict


#: How soon the check runs after a person changes a key, an override or a source's place,
#: so a finding they just acted on stops being shown (it is about 20 s of background work).
RECHECK_SECONDS = 15


def recheck_soon(reason: str) -> None:
    """Queue the check after a person's change. Called after the change has committed."""
    from .scheduler import queue_epg_check
    queue_epg_check(RECHECK_SECONDS, reason)


def account_findings(card: AccountScorecard, proposals: dict, shared) -> list[Finding]:
    """Every finding for one account - pure. `card` is account_scorecard(with_channels=True);
    `proposals` {source_id: [NameProposal]} for this account's channels, as the review page
    proposes them by default; `shared` the account's [SharedId].

    - name matches: channels with no guide that one source's names would guide, the count
      the review page's "only channels with no guide today" view lists;
    - a shared id: the channels whose names say it is not theirs (find_shared_ids());
    - better source: channels another source serves better than their own (choose_source()),
      left out where the user picked the source, where only the account's order separates
      them, or where reach is the reason and the guide already continues that far;
    - adds nothing: a source with a directory that no channel has only here, that serves no
      channel best, that no channel is pinned to, and whose names would guide no channel
      that lacks one."""
    out = []
    unguided: dict[int, int] = {}
    for s in card.sources:
        props = proposals.get(s.source_id, ())
        new = [p for p in props if p.kind == MATCH_NEW]
        unguided[s.source_id] = sum(p.channel.epg_source_id is None for p in new)
        if unguided[s.source_id]:
            out.append(Finding(s.source_id, EPG_FINDING_NAME_MATCHES, None,
                               unguided[s.source_id],
                               {'guided': len(new) - unguided[s.source_id],
                                'disagree': sum(p.kind == MATCH_DISAGREE for p in props)}))
    for f in shared:
        out.append(Finding(f.source_id, EPG_FINDING_SHARED_ID, f.entry.xml_id, len(f.wrong),
                           {'channels': len(f.channels),
                            'name': (f.entry.display_names or [f.entry.xml_id])[0]}))
    best_for = Counter(c.best for c in card.channels)
    # A source channels are pinned to is the user's answer for them; telling them it adds
    # nothing would suggest stopping it, which throws the pins away.
    pinned_to = {c.current for c in card.channels if c.pinned}
    better: dict[int, dict] = {}
    for c in card.channels:
        if (c.pinned or c.current is None or c.best == c.current or c.why in (None, RULE_ORDER)
                or all(cs.source_id != c.current for cs in c.sources)):
            continue
        if c.why == RULE_REACH and c.reach_met:
            # Its guide already continues that far from a later source.
            continue
        b = better.setdefault(c.best, {'count': 0, 'why': Counter(), 'from': Counter()})
        b['count'] += 1
        b['why'][c.why] += 1
        b['from'][c.current] += 1
    for s in card.sources:
        b = better.get(s.source_id)
        if b:
            out.append(Finding(s.source_id, EPG_FINDING_BETTER_SOURCE, None, b['count'],
                               {'why': dict(b['why']),
                                'from': {str(k): n for k, n in b['from'].items()}}))
        if (s.has_directory and not s.only_here and not best_for.get(s.source_id)
                and not unguided[s.source_id] and s.source_id not in pinned_to):
            out.append(Finding(s.source_id, EPG_FINDING_ADDS_NOTHING, None, s.covered, {}))
    return out


_WHY_WORDS = {RULE_OUTVOTED: 'the other sources agree with each other and not with theirs',
              RULE_ONE_TITLE: 'theirs lists one program all day',
              RULE_DOUBLED: 'theirs lists two programs at the same time',
              RULE_REACH: 'it runs further ahead',
              RULE_DETAIL: 'it describes more of its listings'}


def finding_text(f: Finding, names: dict) -> tuple[str, str]:
    """(line, tooltip) for a finding on the Sources card. `names` {source_id: name}."""
    n = f.count
    if f.kind == EPG_FINDING_NAME_MATCHES:
        line = (f'{counted(n, "channel")} with no guide {plural(n, "has", "have")} a name '
                'that matches a channel in this guide.')
        tip = ['Accepting a match gives the channel this guide\'s listings for that name.']
        if f.detail.get('guided'):
            g = f.detail['guided']
            tip.append(f'{counted(g, "channel")} that already {plural(g, "has", "have")} a '
                       f'guide also {plural(g, "matches", "match")} a name.')
        if f.detail.get('disagree'):
            d = f.detail['disagree']
            tip.append(f'{counted(d, "channel")} {plural(d, "has", "have")} a name that points '
                       'at a different id from the provider\'s.')
    elif f.kind == EPG_FINDING_SHARED_ID:
        line = (f'{counted(n, "channel")} with different names share the id {f.subject}, so '
                f'this guide gives them all the schedule of "{f.detail.get("name") or f.subject}".')
        tip = [f'{f.detail.get("channels", n):,} channels on this account read that id.']
    elif f.kind == EPG_FINDING_BETTER_SOURCE:
        line = (f'Would serve {counted(n, "channel")} better than the source '
                f'{plural(n, "its", "their")} guide comes from.')
        tip = []
        for rule, m in sorted(f.detail.get('why', {}).items(), key=lambda x: -x[1]):
            if rule in _WHY_WORDS:
                tip.append(f'{m:,} because {_WHY_WORDS[rule]}')
            else:
                log.warning('EPG finding on source %d: no words for rule %r', f.source_id, rule)
        tip += [f'{m:,} {plural(m, "comes", "come")} from {names.get(int(sid), "another source")} now'
                for sid, m in sorted(f.detail.get('from', {}).items(), key=lambda x: -x[1])]
        tip.append('A channel\'s own page picks its source.')
    elif f.kind == EPG_FINDING_ADDS_NOTHING:
        line = ('Adds nothing to this account: every channel it covers is served as well by '
                'another source.' if n else "Gives none of this account's channels a guide.")
        tip = ['Every source is downloaded at each refresh. One that adds nothing can be '
               'stopped with Stop using.']
    else:
        raise ValueError(f'unknown EPG finding kind {f.kind!r}')
    return line, '\n'.join(tip)


def dashboard_text(findings, names: dict) -> str | None:
    """One account's Dashboard sentence for the findings someone should act on, or None when
    there are none: name matches and shared ids, which leave channels with no guide or the
    wrong one until a person acts. The other two kinds say a choice could be better, which
    the Sources card says. `findings` is the account's [Finding]; `names` {source_id:
    name}. One line per account, since its Sources card lists each source's: the largest
    name-match count, naming its guide, and the channels shared ids mislead - an id read
    through two guides misleads the same channels in both, so each id counts once, at its
    largest."""
    matches = [f for f in findings if f.kind == EPG_FINDING_NAME_MATCHES]
    shared: dict[str, int] = {}
    for f in findings:
        if f.kind == EPG_FINDING_SHARED_ID:
            shared[f.subject] = max(shared.get(f.subject, 0), f.count)
        elif f.kind not in (EPG_FINDING_NAME_MATCHES, EPG_FINDING_BETTER_SOURCE,
                            EPG_FINDING_ADDS_NOTHING):
            raise ValueError(f'unknown EPG finding kind {f.kind!r}')
    parts = []
    if matches:
        top = max(matches, key=lambda f: f.count)
        text = (f'{counted(top.count, "channel")} with no guide '
                f'{plural(top.count, "matches", "match")} a name in '
                f'{names.get(top.source_id, "a guide")}')
        if len(matches) > 1:
            text += f', and more in {counted(len(matches) - 1, "other guide")}'
        parts.append(text)
    if shared:
        n = sum(shared.values())
        ids = ', '.join(sorted(shared))
        parts.append(f'{counted(n, "channel")} {plural(n, "gets", "get")} another channel\'s '
                     f'schedule from a shared id ({ids})')
    return '; '.join(parts) if parts else None


def stored_findings(account_ids) -> dict[int, list]:
    """{account_id: [(Finding, found_at)]} for these accounts, only for sources each still
    reads (a stop that raced the check leaves nothing behind on a page). Two queries."""
    account_ids = list(account_ids)
    out: dict[int, list] = {aid: [] for aid in account_ids}
    if not account_ids:
        return out
    reads = set(db.session.query(EpgSourceSubscription.account_id, EpgSourceSubscription.source_id)
                .filter(EpgSourceSubscription.account_id.in_(account_ids)))
    rows = (db.session.query(EpgFinding.account_id, EpgFinding.source_id, EpgFinding.kind,
                             EpgFinding.subject, EpgFinding.count, EpgFinding.detail,
                             EpgFinding.found_at)
            .filter(EpgFinding.account_id.in_(account_ids))
            .order_by(EpgFinding.account_id, EpgFinding.id))
    for aid, sid, kind, subject, n, detail, at in rows:
        if (aid, sid) not in reads:
            continue
        try:
            parsed = json.loads(detail) if detail else {}
        except ValueError:
            log.warning('EPG finding for account %d and source %d: unreadable detail', aid, sid)
            parsed = {}
        out[aid].append((Finding(sid, kind, subject, n, parsed), at))
    return out


def _replace_findings(account_id: int, findings, now: datetime) -> None:
    """Replace an account's rows. An account or source deleted while the check ran (it reads
    first and writes per account, about 18 s apart on prod) gets none."""
    @retry_on_locked()
    def _replace_and_commit():
        db.session.execute(delete(EpgFinding).where(EpgFinding.account_id == account_id))
        alive = {sid for (sid,) in db.session.query(EpgSource.id).filter(
            EpgSource.id.in_({f.source_id for f in findings}))} if findings else set()
        if db.session.get(Account, account_id) is None:
            alive = set()
        kept = [f for f in findings if f.source_id in alive]
        if kept:
            db.session.bulk_insert_mappings(EpgFinding, [
                {'account_id': account_id, 'source_id': f.source_id, 'kind': f.kind,
                 'subject': f.subject, 'count': f.count,
                 'detail': json.dumps(f.detail) if f.detail else None, 'found_at': now}
                for f in kept])
        db.session.commit()
    _replace_and_commit()


def check_findings(orders: dict, now: datetime) -> int:
    """Find and store every account's findings. `orders` {account_id: [source_id]}. Each
    account's rows are replaced in one retry_on_locked unit; reading and judging happen
    before it. Returns how many were stored."""
    from .config import load_config
    from .epg_sources import (case_sensitive_matching, last_local_day, name_match_review,
                              shared_ids)
    from .tz_utils import get_display_tz
    cs = case_sensitive_matching(load_config())
    tz = get_display_tz()
    source_ids = sorted({sid for order in orders.values() for sid in order})
    proposals: dict[int, dict[int, list]] = {}
    for source in EpgSource.query.filter(EpgSource.id.in_(source_ids)):
        for p in name_match_review(source, cs).proposals:
            proposals.setdefault(p.channel.account_id, {}).setdefault(source.id, []).append(p)
    shared: dict[int, list] = {}
    for f in shared_ids(source_ids, list(orders), cs):
        shared.setdefault(f.account_id, []).append(f)
    db.session.rollback()   # end the reads before the CPU work, so no snapshot is held
    stored = 0
    for aid in orders:
        # Reach is judged by the local day a guide runs through, as the scorecard API judges
        # it, so a finding and the channel's "best here" never name different sources.
        card = account_scorecard(aid, tz, cs, with_channels=True,
                                 day_of=lambda h: last_local_day(h, tz).isoformat())
        db.session.rollback()
        found = account_findings(card, proposals.get(aid, {}), shared.get(aid, []))
        _replace_findings(aid, found, now)
        stored += len(found)
    return stored


# ── Followed sports games per source (dev/changelog/1726) ────────────────────
# Which followed games each source would match, judged by the sports matcher on that source's
# listings alone (sports_match.source_games()). Only for an account reading two or more
# sources: with one, the answer is the matcher's own, which the sports pages already show, and
# a single source is where it costs most (33 s for prod's account 2, one 1.8M-listing feed).

class SourceGamesScore(NamedTuple):
    source_id: int
    through_day: date | None    # the source's typical reach day; None = reaches nothing ahead
    followed: int               # followed games still to come through that day
    listed: int                 # ... that it lists
    listed_later: int           # games it lists after that day
    live: int                   # of `listed`, the ones with a live airing
    channels: int               # channels it lists `listed` on
    only_here: int              # of `listed`, the ones no other source the account reads lists
    event_ids: tuple            # every game it lists, `listed_later` included


def score_source_games(games, reach_days: dict) -> list[SourceGamesScore]:
    """One SourceGamesScore per source in `games.listed` - pure. `reach_days` {source_id:
    the last local day it typically reaches}. Every number but `listed_later` and
    `event_ids` is over the games through that day, so the card's one sentence is one
    window; "only here" asks whether any other source lists the game at all."""
    out = []
    for sid, listed in games.listed.items():
        through = reach_days.get(sid)
        inside = {eid for eid, day in games.followed.items() if through is None or day <= through}
        mine = {eid: g for eid, g in listed.items() if eid in inside}
        others = set().union(*(set(o) for s, o in games.listed.items() if s != sid))
        out.append(SourceGamesScore(
            sid, through, len(inside), len(mine), len(set(listed) - inside),
            sum(1 for g in mine.values() if g.live),
            len(set().union(*(g.channels for g in mine.values()))),
            len(set(mine) - others), tuple(sorted(listed))))
    return out


def _replace_source_games(account_id: int, scores, now: datetime) -> None:
    """Replace an account's rows. An account or source deleted while the check ran gets none,
    as with the findings."""
    @retry_on_locked()
    def _replace_and_commit():
        db.session.execute(delete(EpgSourceGames).where(EpgSourceGames.account_id == account_id))
        alive = {sid for (sid,) in db.session.query(EpgSource.id).filter(
            EpgSource.id.in_({s.source_id for s in scores}))} if scores else set()
        if db.session.get(Account, account_id) is None:
            alive = set()
        kept = [s for s in scores if s.source_id in alive]
        if kept:
            db.session.bulk_insert_mappings(EpgSourceGames, [
                {**{k: v for k, v in s._asdict().items() if k != 'event_ids'},
                 'account_id': account_id, 'event_ids': json.dumps(list(s.event_ids)),
                 'counted_at': now}
                for s in kept])
        db.session.commit()
    _replace_and_commit()


def check_source_games(orders: dict, now: datetime) -> int:
    """Count and store every account's followed games per source. `orders` {account_id:
    [source_id]}. An account reading one source keeps no rows. The matcher's judging happens
    before each account's one retry_on_locked write. Returns the accounts counted."""
    from .epg_sources import source_reach
    from .sports_match import source_games
    from .tz_utils import get_display_tz
    tz = get_display_tz()
    counted_accounts = 0
    for aid, order in orders.items():
        if len(order) < 2:
            _replace_source_games(aid, [], now)
            continue
        games = source_games(aid, order, now)
        reach = {sid: r.day for sid, r in source_reach(order, tz).items()}
        db.session.rollback()
        scores = score_source_games(games, reach) if games.followed else []
        _replace_source_games(aid, scores, now)
        counted_accounts += bool(scores)
    return counted_accounts


def stored_source_games(account_id: int) -> list[tuple[SourceGamesScore, datetime]]:
    """[(SourceGamesScore, counted_at)] for the sources the account still reads. Two queries."""
    reads = {sid for (sid,) in db.session.query(EpgSourceSubscription.source_id).filter(
        EpgSourceSubscription.account_id == account_id)}
    out = []
    for row in EpgSourceGames.query.filter(EpgSourceGames.account_id == account_id).order_by(
            EpgSourceGames.id):
        if row.source_id not in reads:
            continue
        try:
            ids = tuple(json.loads(row.event_ids or '[]'))
        except ValueError:
            log.warning('EPG source games for account %d and source %d: unreadable event ids',
                        account_id, row.source_id)
            ids = ()
        out.append((SourceGamesScore(row.source_id, row.through_day, row.followed, row.listed,
                                     row.listed_later, row.live, row.channels, row.only_here,
                                     ids), row.counted_at))
    return out


def sports_json(account_id: int, now: datetime) -> dict | None:
    """The scorecard API's followed sports games, unformatted, for the EPG Manager: each
    source's stored counts and listed game ids, and the games this account's channels name.
    None when the check has stored none for the account."""
    from .sports_match import name_games
    games = stored_source_games(account_id)
    if not games:
        return None
    listed = set().union(*(g.event_ids for g, _at in games))
    named = name_games(account_id, now)
    return {'sources': [{**g._asdict(), 'through_day': iso_or_none(g.through_day),
                         'event_ids': list(g.event_ids), 'counted_at': iso_or_none(at)}
                        for g, at in games],
            'by_channel_name': {'games': len(named),
                                'channels': len(set().union(*named.values())) if named else 0,
                                'only_by_name': len(set(named) - listed),
                                'event_ids': sorted(named)}}


def followed_noun() -> tuple:
    """(one, many) for the games followed: the sport's own word when every followed league
    reads the same, `events` when they differ (app/sports_words.py)."""
    from .database import SportsFollow, SportsLeague
    from .sports_words import EVENT, noun
    sports = {s for (s,) in db.session.query(SportsLeague.sport).filter(
        SportsLeague.id.in_(select(SportsFollow.league_id)))}
    words = {noun(s) for s in sports}
    return words.pop() if len(words) == 1 else EVENT


def games_text(s: SourceGamesScore, day_label, at_label: str, noun: tuple) -> tuple[str, str]:
    """(line, tooltip) for one source's followed games on the Sources card. `day_label(day)`
    is a day's display text; `at_label` when they were counted."""
    one, many = noun
    when = f'through {day_label(s.through_day)}' if s.through_day else 'still to come'
    line = f'Lists {s.listed:,} of the {counted(s.followed, "followed " + one, "followed " + many)} {when}'
    if s.channels:
        line += f', on {counted(s.channels, "channel")}'
    if s.only_here:
        line += f', {s.only_here:,} only here'
    tip = [f'Followed {many}, judged by the sports matcher on this guide\'s listings alone.']
    if s.listed and not s.live:
        tip.append('Every one of them is a replay; none airs live here.')
    elif s.listed and s.live < s.listed:
        tip.append(f'{s.live:,} of the {counted(s.listed, one, many)} {plural(s.live, "has", "have")} '
                   'a live airing here; the rest only replays.')
    elif s.listed:
        tip.append('Every one of them has a live airing here.')
    if s.only_here:
        tip.append(f'{counted(s.only_here, one, many)} of them no other source on this account lists.')
    if s.listed_later:
        tip.append(f'It also lists {counted(s.listed_later, one, many)} after '
                   f'{day_label(s.through_day)}, not counted above.')
    tip.append(f'Counted {at_label}.')
    return line + '.', '\n'.join(tip)


def name_games_text(named: dict, listed_ids, noun: tuple) -> tuple[str, str] | None:
    """(line, tooltip) for the followed games this account's channels name, or None when
    none do. `named` is sports_match.name_games(); `listed_ids` every game a source lists."""
    if not named:
        return None
    one, many = noun
    channels = set().union(*named.values())
    only = len(set(named) - set(listed_ids))
    line = (f'Read from channel names: {counted(len(named), "followed " + one, "followed " + many)} '
            f'on {counted(len(channels), "channel")}')
    line += f', {only:,} that no guide here lists.' if only else ', every one also in a guide here.'
    tip = ('A channel whose guide has nothing at a game\'s time is read by its name. These belong '
           'to no one source, so they are counted apart from each source\'s line.')
    return line, tip


# ── The check that stores agreement ──────────────────────────────────────────

def _held_listings(channel_ids, source_ids, now) -> dict[int, dict[int, list]]:
    """{channel_id: {source_id: [Listing] by start}} from now on, from both tables."""
    out: dict[int, dict[int, list]] = {}
    for model in (EPGEntry, EpgAlternateEntry):
        q = (db.session.query(model.channel_id, model.source_id, model.title, model.sub_title,
                              model.description, model.start_time, model.stop_time)
             .filter(model.channel_id.in_(list(channel_ids)), model.stop_time > now,
                     of_sources(model, source_ids)))
        for cid, sid, title, sub, desc, start, stop in q:
            out.setdefault(cid, {}).setdefault(sid, []).append(
                Listing(title, sub, desc, start, stop))
    for by_source in out.values():
        for listings in by_source.values():
            listings.sort(key=lambda x: x.start)
    return out


def judge(held: dict[int, list], order, placeholders) -> list[tuple[int, int, object]]:
    """[(earlier, later, Comparison)] for every pair of sources holding listings - pure.
    `placeholders` is the channel's placeholder_names()."""
    present = [sid for sid in order if held.get(sid)]
    return [(a, b, compare_listings(held[a], held[b], placeholders=placeholders))
            for i, a in enumerate(present) for b in present[i + 1:]]


def check_account(account_id: int, order, now: datetime) -> int:
    """Rewrite the stored agreement for one account's channels. Returns the pairs judged.

    Only channels with a guide can hold listings from two sources (the alternates are kept
    for a channel with an active source, §4), so those are read, a chunk at a time. Each
    chunk's rows are replaced in one retry_on_locked unit; comparing happens before it."""
    judged = 0
    if len(order) < 2:
        _clear_account(account_id)
        return 0
    names = dict(db.session.query(Channel.id, Channel.name).filter(
        Channel.account_id == account_id, Channel.hidden.is_(False),
        Channel.epg_source_id.isnot(None)))
    guided = list(names)
    _clear_unguided(account_id)
    for chunk in chunked(guided, _CHUNK):
        held = _held_listings(chunk, order, now)
        db.session.rollback()   # end the read before the CPU work, so no snapshot is held
        found = []
        for cid in chunk:
            for a, b, cmp in judge(held.get(cid, {}), order, placeholder_names([names[cid]])):
                found.append({'channel_id': cid, 'source_id': a, 'other_source_id': b,
                              'verdict': cmp.verdict, 'compared': cmp.compared,
                              'paired': cmp.same + cmp.moved, 'checked_at': now})
            time_mod.sleep(0)   # yield the GIL to request threads between channels
        judged += len(found)

        @retry_on_locked()
        def _replace_and_commit(chunk=chunk, found=found):
            db.session.execute(delete(EpgSourceAgreement)
                               .where(EpgSourceAgreement.channel_id.in_(chunk)))
            if found:
                db.session.bulk_insert_mappings(EpgSourceAgreement, found)
            db.session.commit()

        _replace_and_commit()
    return judged


def _clear_account(account_id: int) -> None:
    @retry_on_locked()
    def _clear_and_commit():
        db.session.execute(delete(EpgSourceAgreement).where(
            EpgSourceAgreement.channel_id.in_(
                select(Channel.id).where(Channel.account_id == account_id))))
        db.session.commit()
    _clear_and_commit()


def _clear_unguided(account_id: int) -> None:
    """A channel hidden or left with no guide since the last check has nothing to compare."""
    @retry_on_locked()
    def _clear_and_commit():
        db.session.execute(delete(EpgSourceAgreement).where(
            EpgSourceAgreement.channel_id.in_(
                select(Channel.id).where(Channel.account_id == account_id,
                                         db.or_(Channel.hidden.is_(True),
                                                Channel.epg_source_id.is_(None))))))
        db.session.commit()
    _clear_and_commit()


def _clear_unread(account_ids) -> None:
    """An account that reads no source any more keeps no agreement, finding or game count."""
    @retry_on_locked()
    def _clear_and_commit():
        accounts = select(Channel.id)
        findings = delete(EpgFinding)
        games = delete(EpgSourceGames)
        if account_ids:
            accounts = accounts.where(Channel.account_id.notin_(account_ids))
            findings = findings.where(EpgFinding.account_id.notin_(account_ids))
            games = games.where(EpgSourceGames.account_id.notin_(account_ids))
        db.session.execute(delete(EpgSourceAgreement).where(
            EpgSourceAgreement.channel_id.in_(accounts)))
        db.session.execute(findings)
        db.session.execute(games)
        db.session.commit()
    _clear_and_commit()


def _shared_id_subjects() -> dict[int, set]:
    """{account_id: {(source_id, shared id)}} as the findings stand."""
    out: dict[int, set] = {}
    for aid, sid, subject in db.session.query(
            EpgFinding.account_id, EpgFinding.source_id, EpgFinding.subject).filter(
            EpgFinding.kind == EPG_FINDING_SHARED_ID):
        out.setdefault(aid, set()).add((sid, subject))
    db.session.rollback()
    return out


def _place_where_flags_moved(before: dict, after: dict) -> None:
    """A guide is not continued through an id unrelated channels share
    (epg_sources.decide_stretches()), and that reads the stored finding. So an account whose
    shared ids changed has its guided channels placed again (dev/changelog/1722)."""
    from .config import load_config
    from .epg_sources import case_sensitive_matching, place_stretches
    moved = [aid for aid in set(before) | set(after) if before.get(aid) != after.get(aid)]
    if not moved:
        return
    cs = case_sensitive_matching(load_config())
    for aid in moved:
        ids = [cid for (cid,) in db.session.query(Channel.id).filter(
            Channel.account_id == aid, Channel.hidden.is_(False),
            Channel.epg_source_id.isnot(None))]
        placed = place_stretches(ids, cs)
        log.info('EPG source check: shared ids changed on account %d, %d channel(s) placed '
                 'again', aid, placed.channels)


def run_check(now: datetime | None = None) -> str:
    """One run over every account that reads an EPG source: the agreement of its sources,
    then its findings, then its followed sports games per source, stored. Takes a KIND_EPG_CHECK ticket; a refusal queues the next try.
    Returns a note for the log."""
    from .scheduler import queue_epg_check
    ticket = admission.try_start(admission.KIND_EPG_CHECK, 'EPG sources')
    if not ticket.granted:
        queue_epg_check(RETRY_MINUTES * 60, 'its last run was refused')
        note = f'EPG source check waiting: {ticket.reason}. Tries again in {RETRY_MINUTES} minutes.'
        log.info(note)
        return note
    started = time_mod.monotonic()
    try:
        now = now or datetime.utcnow()
        orders: dict[int, list] = {}
        for aid, sid in (db.session.query(EpgSourceSubscription.account_id,
                                          EpgSourceSubscription.source_id)
                         .order_by(EpgSourceSubscription.account_id,
                                   EpgSourceSubscription.priority)):
            orders.setdefault(aid, []).append(sid)
        db.session.rollback()
        _clear_unread(list(orders))
        judged = sum(check_account(aid, order, now) for aid, order in orders.items())
        flagged_before = _shared_id_subjects()
        found = check_findings(orders, now)
        _place_where_flags_moved(flagged_before, _shared_id_subjects())
        games_at = time_mod.monotonic()
        games = check_source_games(orders, now)
        note = (f'EPG source check: {counted(judged, "channel and source pair")} compared and '
                f'{counted(found, "finding")} stored over {counted(len(orders), "account")} '
                f'in {time_mod.monotonic() - started:.1f} s; followed sports games counted per '
                f'source on {counted(games, "account")} in {time_mod.monotonic() - games_at:.1f} s.')
        log.info(note)
        return note
    finally:
        admission.release(ticket)
