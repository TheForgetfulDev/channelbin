"""Hiding channels: the effective answer, its one writer, and the display sentence.

A hidden channel is not offered to you - not in either grain of the channel search, not in
any picker built on that engine, not in the counts. It keeps its detail page, reachable
directly by id, which is where you un-hide it.

**Hiding decides where a channel is OFFERED and nothing else.** It never stops a recording,
never removes a guide row, never touches group membership, and never affects which member a
group records from. That is what keeps `Channel.in_guide` meaning exactly one thing.

Four sources stack into one answer, and a channel is hidden if any of them says so:

1. its category matches a GLOB pattern rule,
2. its category was picked by exact name,
3. its name matches a GLOB pattern rule,
4. the user hid that exact channel by hand.

Sources 1-3 are `ChannelHideRule` rows, global or scoped to one account; source 4 is a
column on `channels`. Source 4 overrides the other three in BOTH directions: it can
force-hide a channel no rule matches and force-show one that several do.

**Resolution order is 1, 2, 3, 4, and it is a performance decision rather than a semantic
one** - the four sources OR together, so the only thing order changes is which name lands in
`hidden_reason`. It is worth honoring anyway: categories resolve against the CATEGORY LIST
(1,719 distinct values here) and not against the channel table, so a category GLOB is 1,719
string tests where a name GLOB is 136,940 per pattern. `resolve_rules()` does that first
half; the single UPDATE in `recompute()` does the second.

Patterns are SQLite `GLOB` - `*`, `?` and `[...]`, natively case-sensitive, matched against
the WHOLE string. So `A?` is an `A` and exactly one more character, `A*` is anything starting
with `A`, `*A*` is an `A` anywhere, `*A` ends in one, and `A*Z` starts with `A` and ends with
`Z`. Verified against SQLite itself rather than assumed, and pinned by
`tests/test_channel_hide_rules.py::GlobSemanticsTests`.

**Guide/group membership DEFERS a hide, it never refuses one.** A channel in the TV Guide or
in any channel group stays visible even when a source says hide, and its page says so. Remove
it from the guide or the group and it drops out on its own at the next recompute. Uniform
across all four sources, including a hand-hide: a refusal would make the user remove the
channel from the guide, come back, and hide it again, while a deferral records the intent and
honors it the moment the blocker clears. It is also what lets a bulk hide of 100 channels
where 1 is protected hide 99 and report the 1, rather than failing the batch. Same move the
group format lock makes - a constraint filters where the thing is chosen and never mutates
stored intent.

**Two kinds of column, and they must not blur.** `Channel.hidden_override` is the human's
answer to a judgment call, written only by `set_hidden_override()` and by nothing else, ever.
`Channel.hidden` / `.hidden_reason` / `.hidden_deferred` are the opposite: a derived cache
written only by `recompute()`, never by a human and never by a route. `Account.hidden_
channel_count` is the same second kind, one level up - nothing but a count of `hidden`, kept
current by `refresh_hidden_channel_counts()` from inside `recompute()` itself, and by nothing
else except the missing-channel sweep's own hard delete of hidden rows.

Design, reasoning and measurements: dev/docs/DESIGN-channel-hiding.md, dev/changelog/775
(the schema and source 4), dev/changelog/776 (the rule engine) and dev/changelog/783 (the
counts).
"""
import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import and_, case, delete, func, null, or_, not_, select, update

from . import admission, db
from .database import (Account, Channel, ChannelEvent, ChannelGroupMember, ChannelHideRule,
                       EPGEntry, CHANNEL_HIDE_OVERRIDE_CHANGED, HIDE_TARGETS,
                       HIDE_TARGET_CATEGORY_EXACT, HIDE_TARGET_CATEGORY_GLOB,
                       HIDE_TARGET_NAME_GLOB)
from .db_utils import retry_on_locked

#: `Channel.hidden_reason` values - which source produced the current answer. The three rule
#: sources deliberately share their `ChannelHideRule.target` spelling: one fact ("a category
#: GLOB did this") with one name, rather than a reason vocabulary that has to be mapped onto
#: a target vocabulary every time a surface wants to explain itself.
HIDE_REASON_MANUAL         = 'manual'
HIDE_REASON_CATEGORY_GLOB  = HIDE_TARGET_CATEGORY_GLOB
HIDE_REASON_CATEGORY_EXACT = HIDE_TARGET_CATEGORY_EXACT
HIDE_REASON_NAME_GLOB      = HIDE_TARGET_NAME_GLOB

HIDE_REASON_LABELS = {
    HIDE_REASON_MANUAL: 'you hid it',
    HIDE_REASON_CATEGORY_GLOB: 'its category matches a rule',
    HIDE_REASON_CATEGORY_EXACT: 'its whole category is hidden',
    HIDE_REASON_NAME_GLOB: 'its name matches a rule',
}

#: The longest pattern `validate_pattern()` accepts, matching the column width.
MAX_PATTERN_LENGTH = 512

#: What `set_hidden_override()` will accept, and what each value means.
OVERRIDE_VALUES = {
    None: 'follow the rules',
    True: 'always hidden',
    False: 'always shown',
}


# ---------------------------------------------------------------------------
# The one recompute
# ---------------------------------------------------------------------------

def _in_any_group_expr():
    """"Is this channel in any channel group at all", as a correlated EXISTS.

    Deliberately ANY group, not only one that is in the guide: a channel sitting in a
    health-check-only group is one the user curated and is monitoring, so hiding it out from
    under them would be the same mistake the duplicate keep-rule already refuses to make
    (dev/changelog/759).

    Spelled here rather than imported from `channel_search.in_any_group_expr()` because that
    module is the search engine and this one is read by the sync: the two must not import
    each other to share four lines of correlated SELECT.
    """
    return select(ChannelGroupMember.id).where(
        ChannelGroupMember.channel_id == Channel.id).correlate(Channel).exists()


def protection_expr():
    """"Is this channel protected from being hidden" - in the TV Guide, or in any group.

    A guide row belonging to a GROUP this channel is a member of does not appear here and
    does not need to: the membership itself already protects the channel.
    """
    return or_(Channel.in_guide.is_(True), _in_any_group_expr())


@dataclass(frozen=True)
class ResolvedRules:
    """The rule set, reduced to what a SQL predicate over `channels` needs.

    Category rules arrive here already resolved into concrete `(account_id, category_name)`
    pairs, which is the whole point of the type: the expensive half of a category rule is
    deciding which of the 1,719 categories it matches, and doing that once per pass instead
    of once per channel row is the ordering decision the module docstring records.

    A pair's `account_id` may be None, meaning "this category name, on any account" - that is
    how a global exact pick is spelled. Glob pairs always carry a concrete account, because
    they were read back out of the channel table.
    """
    category_glob_pairs: frozenset = field(default_factory=frozenset)
    category_exact_pairs: frozenset = field(default_factory=frozenset)
    #: `(account_id_or_None, pattern)` - matched against `Channel.name` in the UPDATE itself,
    #: since there is no smaller list than the channel table to match a channel name against.
    name_globs: tuple = ()


def load_rules(enabled_only=True) -> list:
    """Every hide rule, oldest first. `enabled_only` is what the materializer wants; the
    stats refresh and the API want the disabled ones too, since a disabled rule still gets to
    say what it would hide."""
    q = ChannelHideRule.query
    if enabled_only:
        q = q.filter(ChannelHideRule.enabled.is_(True))
    return q.order_by(ChannelHideRule.id).all()


def _scope_terms(channel_ids=None, account_id=None) -> list:
    """The WHERE terms that narrow a pass to part of the channel table. Shared by the UPDATE
    and by the category resolution, so a scoped pass never resolves categories it cannot
    reach - which is what keeps a guide toggle off the full-table scan."""
    terms = []
    if channel_ids is not None:
        terms.append(Channel.id.in_(sorted({int(cid) for cid in channel_ids})))
    if account_id is not None:
        terms.append(Channel.account_id == int(account_id))
    return terms


def resolve_rules(rules=None, scope_terms=()) -> ResolvedRules:
    """Turn rule rows into `ResolvedRules`, resolving category GLOBs against the category
    list rather than against the channel table.

    One query, whatever the rule count: a CTE over `SELECT DISTINCT account_id,
    category_name` with every category pattern OR'd against it. Measured on the live
    database (136,940 channels, 1,719 categories): 0.242s for ten patterns through the CTE
    against 0.440s for the same ten asked one at a time, and a name GLOB - which has no
    smaller list to run against - costs 0.088s per pattern on its own.

    Exact picks need no query at all: the pattern IS the category name, so the pair is the
    rule row.
    """
    rules = load_rules() if rules is None else list(rules)
    glob_rules = [r for r in rules if r.target == HIDE_TARGET_CATEGORY_GLOB]
    exact_pairs = frozenset(
        (r.account_id, r.pattern) for r in rules if r.target == HIDE_TARGET_CATEGORY_EXACT)
    name_globs = tuple(
        (r.account_id, r.pattern) for r in rules if r.target == HIDE_TARGET_NAME_GLOB)

    glob_pairs = frozenset()
    if glob_rules:
        cats = (select(Channel.account_id, Channel.category_name)
                .where(Channel.category_name.isnot(None), *scope_terms)
                .distinct().cte('hide_rule_categories'))
        terms = []
        for rule in glob_rules:
            term = cats.c.category_name.op('GLOB')(rule.pattern)
            if rule.account_id is not None:
                term = and_(cats.c.account_id == rule.account_id, term)
            terms.append(term)
        rows = db.session.execute(
            select(cats.c.account_id, cats.c.category_name).where(or_(*terms))).all()
        glob_pairs = frozenset((row[0], row[1]) for row in rows)

    return ResolvedRules(category_glob_pairs=glob_pairs, category_exact_pairs=exact_pairs,
                         name_globs=name_globs)


def _pairs_predicate(pairs):
    """`(account_id, category_name)` pairs as one predicate, grouped so each account's names
    ride in a single IN list.

    The IN lists are bounded by the number of distinct (account, category) pairs that exist -
    1,902 on the live database against SQLite's 32,766 bind-variable ceiling, and that is the
    worst case where every category is hidden on every account. An `account_id` of None means
    the name matches on any account.
    """
    if not pairs:
        return db.false()
    by_account = {}
    for account_id, category_name in pairs:
        by_account.setdefault(account_id, set()).add(category_name)
    terms = []
    for account_id in sorted(by_account, key=lambda a: (a is not None, a)):
        names = sorted(by_account[account_id])
        term = Channel.category_name.in_(names)
        if account_id is not None:
            term = and_(Channel.account_id == account_id, term)
        terms.append(term)
    return or_(*terms)


def _name_globs_predicate(name_globs):
    """The name patterns as one predicate. Evaluated per channel row because a channel name
    has no smaller list to be matched against - unlike a category, which has 1,719."""
    if not name_globs:
        return db.false()
    terms = []
    for account_id, pattern in name_globs:
        term = Channel.name.op('GLOB')(pattern)
        if account_id is not None:
            term = and_(Channel.account_id == account_id, term)
        terms.append(term)
    return or_(*terms)


def _rule_predicates(resolved: ResolvedRules):
    """The three rule sources as three predicates, in resolution order. Separate rather than
    pre-OR'd because `hidden_reason` has to name WHICH one answered."""
    return (_pairs_predicate(resolved.category_glob_pairs),
            _pairs_predicate(resolved.category_exact_pairs),
            _name_globs_predicate(resolved.name_globs))


def _wants_hidden_expr(resolved: ResolvedRules):
    """"Does any source say hide this channel", before protection is considered.

    The human's override is source 4 and wins in both directions: True hides whatever the
    rules say, False shows whatever they say, and only NULL - "follow the rules" - lets
    sources 1-3 be consulted at all.
    """
    cat_glob, cat_exact, name_glob = _rule_predicates(resolved)
    return or_(Channel.hidden_override.is_(True),
               and_(Channel.hidden_override.is_(None),
                    or_(cat_glob, cat_exact, name_glob)))


def _reason_expr(resolved: ResolvedRules):
    """Which source's name goes in `hidden_reason`.

    The override comes first because it wins, not because it is source 4: its second case
    catches a force-SHOWN channel, whose reason is NULL even though a rule matches it -
    saying "hidden by a category rule" about a channel sitting in plain sight is exactly the
    kind of number a user cannot explain. The three rule cases then run in resolution order.
    """
    return case(
        (Channel.hidden_override.is_(True), HIDE_REASON_MANUAL),
        (Channel.hidden_override.isnot(None), null()),
        *[(pred, reason) for pred, reason in zip(
            _rule_predicates(resolved),
            (HIDE_REASON_CATEGORY_GLOB, HIDE_REASON_CATEGORY_EXACT, HIDE_REASON_NAME_GLOB))],
        else_=null())


def recompute(channel_ids=None, account_id=None, resolved=None) -> int:
    """Rewrite `hidden` / `hidden_reason` / `hidden_deferred` from the sources, drop the EPG
    entries of whatever is hidden afterwards, and refresh `Account.hidden_channel_count` for
    every account this pass touched. The ONE writer of those three columns - and, because
    hidden_channel_count is nothing but a count of `hidden`, refreshing it anywhere else would
    be a second writer of a fact this function already owns.

    The EPG delete rides here rather than at the doors that hide things - see
    `purge_hidden_epg()` for why - so a channel being hidden and its guide data going away
    are one transaction with no ordering to get wrong.

    `channel_ids` scopes the pass to the channels whose inputs just moved and `account_id`
    scopes it to one account (what a sync wants, where an id list would be 12,696 long);
    both None recomputes the whole table. Returns the number of rows the statement touched.

    `resolved` is a `ResolvedRules` the caller already built - pass it only when it was
    resolved over a scope at least as wide as this call's, since a narrower resolution has
    categories missing from it and would silently un-hide rows. Omitted, it is resolved here
    against this call's own scope.

    Takes no admission ticket. This is the small, hot half - a guide toggle or a group
    membership moving - and it runs inside the caller's request. `materialize()` is the
    ticketed whole-table entry point.

    **One statement, no batching.** The whole pass is a single UPDATE, so there is no state
    in which some rows are answered against the new inputs and some against the old - which
    is what would otherwise need an obligation ledger to survive a crash, and would make
    "the column is set, so the pass must have run" a lie a resumed run could believe.

    Does NOT commit: the caller owns the commit, so the whole read-modify-write - moving the
    input and rewriting the cache - stays inside one `retry_on_locked` unit. Same convention
    as `channel_groups.set_participation()` and `database.add_recording_event()`.

    Rows are updated in bulk with `synchronize_session=False`, so any `Channel` the caller is
    already holding carries a stale `hidden` until the commit expires it. Read the new value
    after committing, never before.
    """
    # The inputs are routinely pending ORM changes made moments ago by the caller - a guide
    # row just dropped, an override just set. This pass reads them in SQL, so they have to be
    # on the connection before it runs. Explicit rather than leaning on autoflush: whether a
    # given statement autoflushes is a detail of how it was built, and getting it wrong here
    # is silent - the pass succeeds and answers against the previous state.
    db.session.flush()

    if channel_ids is not None:
        channel_ids = {int(cid) for cid in channel_ids}
        if not channel_ids:
            return 0
    scope_terms = _scope_terms(channel_ids, account_id)
    if resolved is None:
        resolved = resolve_rules(scope_terms=scope_terms)

    protected = protection_expr()
    wants = _wants_hidden_expr(resolved)

    stmt = update(Channel).values(
        hidden=case((and_(wants, not_(protected)), True), else_=False),
        hidden_deferred=case((and_(wants, protected), True), else_=False),
        hidden_reason=_reason_expr(resolved),
    )
    if scope_terms:
        stmt = stmt.where(*scope_terms)
    result = db.session.execute(stmt.execution_options(synchronize_session=False))
    purge_hidden_epg(scope_terms)

    # Which accounts this pass could have moved the hidden count for, account_id's own
    # column never changes so this is exactly as safe to read after the UPDATE as before it.
    if account_id is not None:
        touched_account_ids = [int(account_id)]
    elif channel_ids is not None:
        touched_account_ids = [
            row[0] for row in db.session.query(Channel.account_id)
            .filter(Channel.id.in_(channel_ids)).distinct().all()]
    else:
        touched_account_ids = None   # whole table: every account
    refresh_hidden_channel_counts(touched_account_ids)
    return result.rowcount or 0


def refresh_hidden_channel_counts(account_ids=None) -> None:
    """Refresh `Account.hidden_channel_count` from `Channel.hidden` right now, for the given
    accounts or every account when `account_ids` is None.

    One UPDATE with a correlated subquery per account row - never a Python per-account loop -
    so it costs the same whether it is following a single-channel hand-hide or a whole-table
    rule materialize. Called from `recompute()` itself so the two numbers move together; the
    one other caller is the missing-channel sweep, which deletes hidden rows outright without
    going through `recompute()` at all (app/routes/channels.py `missing_delete`).

    Does NOT commit - the caller owns the commit, same convention as `recompute()`.
    """
    if account_ids is not None and not account_ids:
        return
    stmt = update(Account).values(
        hidden_channel_count=(
            select(func.count(Channel.id))
            .where(Channel.account_id == Account.id, Channel.hidden.is_(True))
            .scalar_subquery()))
    if account_ids is not None:
        stmt = stmt.where(Account.id.in_(set(account_ids)))
    db.session.execute(stmt.execution_options(synchronize_session=False))


def purge_hidden_epg(scope_terms=()) -> int:
    """Delete the EPG entries of channels that are hidden right now. Returns the row count.

    Runs inside `recompute()`'s own statement pair rather than as a hook of its own, and that
    placement is the point: a channel can transition to hidden through four different doors
    (a rule saved, a hand-hide, a sync, a guide row or group membership going away that
    releases a deferred hide), and a delete hung off any subset of them is a delete that a
    fifth door skips. There is exactly one writer of `hidden`, so putting the delete beside
    it makes "hidden" and "carries no EPG" the same transaction.

    **A recompute from the top, never a decrement.** It asks which channels are hidden NOW
    rather than which ones just changed, so running it twice is a no-op and a retry that
    re-runs the whole closure cannot double anything - the shape CLAUDE.md's "already done is
    a fact you recorded" rule demands of anything re-entered after a crash.

    A subquery rather than a materialized id list: SQLITE_MAX_VARIABLE_NUMBER is 32,766 and a
    realistic rule set hides ~62,000 channels, double the ceiling (dev/docs/BUGS.md
    2026-08-15). Same reason `_import_xmltv`'s own delete is written this way.

    Un-hiding does NOT bring the entries back - nothing here restores them, and the next sync
    for that account is what refills the guide. That gap is disclosed rather than papered
    over: `hide_state()` carries the sentence and the channel page renders it.
    """
    hidden_ids = select(Channel.id).where(Channel.hidden.is_(True))
    if scope_terms:
        hidden_ids = hidden_ids.where(*scope_terms)
    result = db.session.execute(
        delete(EPGEntry).where(EPGEntry.channel_id.in_(hidden_ids))
        .execution_options(synchronize_session=False))
    return result.rowcount or 0


@dataclass(frozen=True)
class MaterializeResult:
    """What a materialize attempt did. `granted` False means it never ran and `reason` names
    what blocked it, in admission's own prose - ready to hand straight to a surface."""
    granted: bool
    rows: int = 0
    reason: str = ''


def materialize(label: str, channel_ids=None, account_id=None, force=False,
                stats=True) -> MaterializeResult:
    """Recompute the hidden answer over a whole table (or a whole account) and commit it,
    holding an admission ticket for the duration.

    The ticketed entry point, and the only one: this is bulk work over 136,940 rows, so it
    asks `admission.try_start()` rather than checking whether anything else is running and
    then starting - the check-then-act shape that lost a real race (dev/changelog/679).

    `force=True` registers unconditionally, for work that is the tail of an already-admitted
    run: an account sync calls this between its channel upsert and its EPG import, and being
    refused by its own sync ticket would be absurd.

    Two commits, so two separately-retried closures rather than one decorator over both: a
    retry re-runs its whole closure from the top, and both of these are recomputes rather
    than increments, so re-running either is a no-op rather than a doubled number.

    A refusal is returned, never raised and never swallowed - the caller routes it to a
    surface and queues a retry. Nothing is lost by one: the rules are already committed, so
    a later pass reaches the same answer.
    """
    ticket = admission.try_start(admission.KIND_HIDING, label, force=force)
    if not ticket.granted:
        return MaterializeResult(granted=False, reason=ticket.reason)
    try:
        @retry_on_locked()
        def _recompute_and_commit():
            n = recompute(channel_ids=channel_ids, account_id=account_id)
            db.session.commit()
            return n

        rows = _recompute_and_commit()

        if stats:
            @retry_on_locked()
            def _refresh_stats_and_commit():
                refresh_rule_stats()
                db.session.commit()

            _refresh_stats_and_commit()
        return MaterializeResult(granted=True, rows=rows)
    finally:
        admission.release(ticket)


# ---------------------------------------------------------------------------
# What each rule matches, for the preview and for the per-rule stats
# ---------------------------------------------------------------------------

def match_predicate(target: str, pattern: str, account_id=None):
    """"Would a rule of this shape match this channel", as a predicate over `channels`.

    The per-row spelling of what `resolve_rules()` answers per category. The two are the same
    question - a channel's category matching a GLOB is the same fact as the channel's
    category being in the set of categories that GLOB matches - and they are spelled twice
    only because the materializer can afford to resolve categories once for the whole table
    while a preview of an unsaved pattern has no set to resolve into. That they agree is not
    left to argument:
    `tests/test_channel_hide_rules.py::PreviewAgreesWithTheMaterializerTests` asserts it.
    """
    if target == HIDE_TARGET_NAME_GLOB:
        term = Channel.name.op('GLOB')(pattern)
    elif target == HIDE_TARGET_CATEGORY_GLOB:
        term = Channel.category_name.op('GLOB')(pattern)
    elif target == HIDE_TARGET_CATEGORY_EXACT:
        term = Channel.category_name == pattern
    else:
        raise ValueError(f'unknown hide-rule target {target!r}')
    if account_id is not None:
        return and_(Channel.account_id == int(account_id), term)
    return term


def validate_pattern(target: str, pattern) -> str:
    """Check a target/pattern pair, returning the pattern to store. Raises ValueError.

    Deliberately does NOT strip whitespace: `DE: ` and `RO| ` are real category prefixes on
    real accounts here, and a trailing space is load-bearing in both. Only a genuinely empty
    pattern is refused, because "hides everything" is caught by counting what the rule
    matches rather than by guessing from its punctuation - `[a-zA-Z0-9]*` is as total as `*`
    and no syntax check would say so.
    """
    if target not in HIDE_TARGETS:
        raise ValueError(f'target must be one of {", ".join(HIDE_TARGETS)}')
    if not isinstance(pattern, str):
        raise ValueError('pattern must be a string')
    if not pattern:
        raise ValueError('pattern cannot be empty')
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError(f'pattern cannot be longer than {MAX_PATTERN_LENGTH} characters')
    return pattern


def preview(target: str, pattern: str, account_id=None, sample_limit=20) -> dict:
    """What a rule would hide, without saving it.

    `*AR*` matches 13,920 channel names on this database, among them `INFOWARS TV`,
    `PARAMOUNT HD`, `HALLMARK HD` and `CBS CARDINALS PHOENIX AZ` - all of them legitimate
    input, none of them what the person typing it meant. That is the whole reason this
    exists, and why the UI does not let a pattern be saved before showing it.

    `hides_everything` is measured, not inferred: the rule matches every channel in its own
    scope. The save path refuses that without an explicit confirmation.
    """
    validate_pattern(target, pattern)
    matched = match_predicate(target, pattern, account_id)
    protected = protection_expr()

    scope = [] if account_id is None else [Channel.account_id == int(account_id)]
    scope_total = db.session.execute(
        select(func.count()).select_from(Channel).where(*scope)).scalar() or 0
    count, deferred = db.session.execute(
        select(func.count(),
               func.sum(case((protected, 1), else_=0))).select_from(Channel).where(matched)
    ).one()
    count = count or 0

    sample = db.session.execute(
        select(Channel.id, Channel.name, Channel.category_name, Channel.account_id)
        .where(matched).order_by(Channel.name, Channel.id).limit(sample_limit)).all()

    out = {
        'matched': count,
        'deferred': int(deferred or 0),
        'scope_total': scope_total,
        'hides_everything': bool(scope_total) and count >= scope_total,
        'sample': [{'id': r[0], 'name': r[1], 'category_name': r[2], 'account_id': r[3]}
                   for r in sample],
    }
    if target in (HIDE_TARGET_CATEGORY_GLOB, HIDE_TARGET_CATEGORY_EXACT):
        # Which categories it caught, not only which channels - the answer a person
        # reviewing a category pattern is actually reading.
        cats = db.session.execute(
            select(Channel.category_name).where(matched, Channel.category_name.isnot(None))
            .distinct().order_by(Channel.category_name).limit(sample_limit)).all()
        out['categories'] = [r[0] for r in cats]
    return out


def category_list(account_id=None) -> list:
    """Every provider category with its channel count, name-sorted. One GROUP BY, 1,719 rows
    here - the list a person picks exact categories out of."""
    scope = [] if account_id is None else [Channel.account_id == int(account_id)]
    rows = db.session.execute(
        select(Channel.account_id, Channel.category_name, func.count())
        .where(Channel.category_name.isnot(None), *scope)
        .group_by(Channel.account_id, Channel.category_name)
        .order_by(Channel.category_name)).all()
    return [{'account_id': r[0], 'category_name': r[1], 'channel_count': r[2]} for r in rows]


def refresh_rule_stats(rules=None) -> int:
    """Recount what each rule matches, and how much of that is being kept visible.

    A DISPLAY cache and nothing more - `Channel.hidden` is the authority for what is hidden,
    and this never writes it. A rule's count is what its own pattern matches, independently
    of every other rule and of anybody's override, because that is the question a person
    reading one line of a rule list is asking.

    Two scans for the whole rule set, not two per rule. Category counts come from one GROUP
    BY over `(account_id, category_name)` (0.310s measured, 1,902 groups), and every name
    pattern rides in a single scan carrying two `SUM(CASE ...)` columns each - 0.350s for ten
    patterns against 0.893s for the same ten asked separately. Disabled rules are counted
    too: a rule that is switched off still gets to say what it would hide.

    Does NOT commit - the caller owns the commit, same convention as `recompute()`.
    """
    rules = load_rules(enabled_only=False) if rules is None else list(rules)
    if not rules:
        return 0
    db.session.flush()
    now = datetime.utcnow()
    protected = protection_expr()

    category_rules = [r for r in rules
                      if r.target in (HIDE_TARGET_CATEGORY_GLOB, HIDE_TARGET_CATEGORY_EXACT)]
    per_category = {}
    if category_rules:
        rows = db.session.execute(
            select(Channel.account_id, Channel.category_name, func.count(),
                   func.sum(case((protected, 1), else_=0)))
            .where(Channel.category_name.isnot(None))
            .group_by(Channel.account_id, Channel.category_name)).all()
        per_category = {(r[0], r[1]): (r[2], int(r[3] or 0)) for r in rows}

    for rule in category_rules:
        if rule.target == HIDE_TARGET_CATEGORY_EXACT:
            names = {rule.pattern}
        else:
            # One indexed lookup per GLOB rule against the distinct category values. The
            # cold path can afford it, and it is what gives each rule its OWN count, which
            # the union `resolve_rules()` builds deliberately cannot.
            names = {row[0] for row in db.session.execute(
                select(Channel.category_name)
                .where(Channel.category_name.op('GLOB')(rule.pattern)).distinct()).all()}
        total = deferred = 0
        for (account_id, category_name), (count, prot) in per_category.items():
            if category_name not in names:
                continue
            if rule.account_id is not None and account_id != rule.account_id:
                continue
            total += count
            deferred += prot
        rule.match_count, rule.deferred_count, rule.counted_at = total, deferred, now

    name_rules = [r for r in rules if r.target == HIDE_TARGET_NAME_GLOB]
    if name_rules:
        columns = []
        for rule in name_rules:
            matched = match_predicate(rule.target, rule.pattern, rule.account_id)
            columns.append(func.sum(case((matched, 1), else_=0)))
            columns.append(func.sum(case((and_(matched, protected), 1), else_=0)))
        totals = db.session.execute(select(*columns).select_from(Channel)).one()
        for i, rule in enumerate(name_rules):
            rule.match_count = int(totals[i * 2] or 0)
            rule.deferred_count = int(totals[i * 2 + 1] or 0)
            rule.counted_at = now

    return len(rules)


# ---------------------------------------------------------------------------
# The one writer of the human's answer
# ---------------------------------------------------------------------------

def set_hidden_override(channel, value, surface='channel_page') -> bool:
    """Move one channel's hide/show override and log the move.

    THE only writer of `Channel.hidden_override`, and reached only from a human action: it
    stores the user's answer to a judgment call, so CLAUDE.md's participation-switch rule
    applies to it directly - no background job, sweep or reconcile pass may write it. A
    channel that a rule matches is filtered where channels are offered, never force-hidden
    here, which is what lets it become visible again on its own when the rule changes.

    `value` is True (always hide), False (always show), or None (follow the rules).

    Mutates and adds the `ChannelEvent` without committing, and does NOT recompute - the
    caller does both, so one `retry_on_locked` unit covers the input and the cache together.
    Returns True when the override moved, False for a no-op: a switch that did not move is
    not something that happened, and logging it would fill the Activity Timeline with lines
    saying nothing changed.

    `tests/test_static_invariants.py::HideOverrideWriteBypassTests` is what keeps there being
    one writer, rather than this docstring.
    """
    # `is`, not `in`: True and 1 hash equal, so a membership test would quietly accept an
    # int and store it in a column three other functions read as a tri-state.
    if not any(value is v for v in OVERRIDE_VALUES):
        raise ValueError(f'hidden_override must be True, False or None, not {value!r}')
    was = channel.hidden_override
    if was is not None:
        was = bool(was)   # SQLite hands back 0/1 on a row loaded from a raw INSERT
    if was is value:
        return False
    channel.hidden_override = value  # hide-override-write-ok: the canonical writer
    detail = (f'Hide override set to "{OVERRIDE_VALUES[value]}" by hand '
              f'(was "{OVERRIDE_VALUES[was]}")')
    db.session.add(ChannelEvent(
        channel_id=channel.id, event_type=CHANNEL_HIDE_OVERRIDE_CHANGED, detail=detail,
        extra_data=json.dumps({'old': was, 'new': value, 'source': 'user',
                               'surface': surface})))
    return True


# ---------------------------------------------------------------------------
# The display answer
# ---------------------------------------------------------------------------

def hide_state(channel, in_group=False, epg_gap=False) -> dict:
    """What a surface should say about this channel's hidden state.

    A pure function of what it is handed - no config, no queries, no ORM navigation - so a
    template or a row builder may call it once per row without turning a page into per-row
    I/O. `in_group` is the caller's precomputed "this channel is in at least one channel
    group", hoisted per request rather than looked up here.

    `epg_gap` is likewise precomputed: "this channel is visible, carries an EPG id, and holds
    no EPG entries at all". Hiding a channel deletes its entries and un-hiding does not bring
    them back, so a just-un-hidden channel has an empty guide until its account next syncs,
    and saying nothing about that would leave the user reading an empty guide row as a broken
    channel (dev/changelog/781). Reported as a fact rather than a sentence because the
    sentence needs the account's next sync time rendered in the user's timezone, which is
    exactly the config read this function must not do.

    Keys: `hidden` (the effective answer), `deferred`, `override`, `reason`, `epg_gap`,
    `protected_by` ('guide', 'group', 'both' or ''), and `label` - the one sentence every
    surface shows, built here so no template re-derives it.
    """
    hidden = bool(channel.hidden)
    deferred = bool(channel.hidden_deferred)
    override = channel.hidden_override
    reason = channel.hidden_reason or None

    in_guide = bool(channel.in_guide)
    protected_by = ('both' if in_guide and in_group
                    else 'guide' if in_guide
                    else 'group' if in_group
                    else '')

    if deferred:
        because = {'both': 'it is in the TV Guide and in a channel group',
                   'guide': 'it is in the TV Guide',
                   'group': 'it is in a channel group'}.get(protected_by, 'it is protected')
        label = f'Hidden. Kept visible because {because}.'
    elif hidden:
        label = f'Hidden - {HIDE_REASON_LABELS.get(reason, "a rule matched it")}.'
    elif override is False:
        label = 'Always shown. Rules cannot hide this channel.'
    else:
        label = ''

    return {'hidden': hidden, 'deferred': deferred, 'override': override, 'reason': reason,
            'protected_by': protected_by, 'label': label, 'epg_gap': bool(epg_gap)}


def channels_in_any_group(channel_ids) -> set:
    """The subset of `channel_ids` that hold at least one group membership - one query, for
    a caller that is about to ask `hide_state()` about several channels."""
    ids = sorted({int(cid) for cid in channel_ids})
    if not ids:
        return set()
    rows = db.session.execute(
        select(ChannelGroupMember.channel_id)
        .where(ChannelGroupMember.channel_id.in_(ids)).distinct()).all()
    return {row[0] for row in rows}
