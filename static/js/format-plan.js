/* Shared client helper for the auto-select-format feature (app/channel_groups.py::
   plan_format_selection, GET /api/channel-groups/<id>/format-plan). Used by
   create-group-modal.js (auto-select at creation time) and, later, the existing-group
   picker modal - written once here so neither reimplements the fetch, the option
   labels, or the bucket table.

   Top-level pure functions only (no DOM access) so they stay Node-harness testable -
   see tests/test_format_plan_js.py, same technique as tests/test_check_modal_js.py.
   Depends on util.js (escHtml, jsonFetch) at call time, not at definition time.
*/

/* The four bucket-ranking strategies, which are the only ones the create-group modal's
   auto-select offers - a group being created has no standing setting yet. Keys only:
   every label in this app now comes from GROUP_FORMAT_STRATEGIES below, so the same
   strategy cannot be named two things on two screens. */
const FORMAT_STRATEGY_KEYS = [
  'highest_bitrate', 'highest_resolution', 'most_channels', 'balanced',
];

/* The plan is always over each member's own latest test regardless of job, because the
   engine that would apply it reads the same thing - a job-scoped preview and an any-job
   engine returned different winners indefinitely, with nothing on screen explaining why
   (dev/changelog/890). There is deliberately no jobId parameter any more.

   `opts.rankScope === 'all'` ranks over every member instead of the recording-enabled
   ones; only the create/clone preview wants it, and only because the group it describes
   does not exist yet. */
function fetchFormatPlan(groupId, opts) {
  const qs = (opts && opts.rankScope === 'all') ? '?rank_scope=all' : '';
  return jsonFetch(`/api/channel-groups/${groupId}/format-plan${qs}`);
}

/* e.g. "Highest bitrate - 1920x1080 @ 60 (27 channels)". When the strategy has no
   winning format, says so instead of rendering a blank option (CLAUDE.md: failure
   paths must be observable). */
function formatPlanOptionLabel(plan, strategy, derived) {
  const label = groupStrategyLabel(strategy);
  /* The three values that do not resolve to a measured bucket say what they DO instead
     of reporting "no eligible format", which for them would be a true sentence about the
     wrong question. */
  if (strategy === 'health_check_only') return `${label} - not a recording source`;
  if (strategy === 'unmanaged') return `${label} - any format, mixed`;
  if (strategy === 'manual') return `${label} - you pick the format`;
  const entry = formatPlanEntry(plan, strategy, null, derived);
  if (!entry || !entry.key) return `${label} - no eligible format`;
  return `${label} - ${entry.label} (${formatPlanCounts(entry)})`;
}

/* The parenthetical after a format label: how many members measure it, how many of those
   the group would actually record from, and whether every one of them is warning.

   All three are here because all three were asked for by name after a picker offered
   "Highest bitrate - 3840x2160 @ 50 (5 channels)" where the five were every one of them
   warning, and where turning Recording off on all five left the count unchanged
   (dev/changelog/890). A count the user cannot act on is principle 1's own example of a
   number worth less than none. */
function formatPlanCounts(entry) {
  const n = entry.count || 0;
  const parts = [`${n} channel${n === 1 ? '' : 's'}`];
  // Omitted when it equals the total: on a group where every member records, "5 channels,
  // 5 recording" is noise. It is only ever news when the two differ.
  if (entry.rank_count != null && entry.rank_count !== n) {
    parts.push(`${entry.rank_count} recording`);
  }
  if (n > 0 && entry.pass_count === 0) parts.push('all warning');
  return parts.join(', ');
}

/* ── The standing group setting (DESIGN-channel-groups-model.md §4.4) ─────────
   ChannelGroup.format_strategy's eight values, in dropdown order, each with the one
   plain sentence of help text §4.4 requires. `balanced` is named there specifically -
   its own name does not say what it does, and a setting whose owner cannot say what it
   does is the number-the-user-cannot-explain principle 1 rates worse than no number -
   but every value ships with one, not just that one.

   Only the middle four are the bucket-ranking engine (app/channel_groups.py::
   FORMAT_STRATEGIES). The other four are answered outside it and always have been:
   health_check_only and unmanaged manage no format at all, highest_score follows the
   healthiest member, and manual is whatever the user pinned. */
const GROUP_FORMAT_STRATEGIES = [
  ['health_check_only', 'Health check only',
   'This group is not a recording source. Its members are tested and nothing else. ' +
   'Choose any other strategy to record from it.'],
  ['highest_score', "Healthiest member's format",
   'No format is pinned. Whichever member is healthiest serves the group, and the group ' +
   'format follows it.'],
  ['highest_bitrate', 'Highest bitrate',
   'Locks to the format whose healthy members carry the highest median bitrate.'],
  ['highest_resolution', 'Highest resolution',
   'Locks to the format with the largest picture among healthy members.'],
  ['most_channels', 'Most members',
   'Locks to the format that leaves the most healthy members eligible, with the higher ' +
   'resolution breaking a tie.'],
  ['balanced', 'Balanced',
   'The highest-bitrate format among those holding at least 60% as many healthy channels ' +
   'as the largest format.'],
  ['manual', 'Pinned format',
   'You pin the format. Nothing moves it, and a health check never overrides it.'],
  ['unmanaged', 'No format management',
   'Records from whichever member ranks best, whatever its format. Members may differ in ' +
   'resolution or frame rate, which can produce a file that plays back wrong.'],
];

function groupStrategyLabel(strategy) {
  const found = GROUP_FORMAT_STRATEGIES.find(([key]) => key === strategy);
  return found ? found[1] : strategy;
}

function groupStrategyHelp(strategy) {
  const found = GROUP_FORMAT_STRATEGIES.find(([key]) => key === strategy);
  return found ? found[2] : '';
}

/* Does this strategy enforce a format on member selection? False for the two values
   that manage none - mirrors app/channel_groups.py::group_manages_format(), which is
   the authority; this is the client's read of the same two names. */
function groupStrategyManagesFormat(strategy) {
  return strategy !== 'health_check_only' && strategy !== 'unmanaged';
}

/* The winning bucket for any of the eight values, not just the four the server's
   `strategies` map answers for.

   `pin` is the format a dialog is CURRENTLY offering, which is not yet the group's
   stored lock - the Settings modal lets you pick one and only writes it on Save, so
   without this the preview under the picker answers for the stored value and the winner
   marker does not move until after you commit. `derived` is the group's effective
   reference (payload.reference_key's label), which is what highest_score follows. */
function formatPlanEntry(plan, strategy, pin, derived) {
  if (strategy === 'health_check_only' || strategy === 'unmanaged') return null;
  const buckets = (plan && plan.buckets) || [];
  const bucketFor = (key) => buckets.find((b) => b.label === key);
  /* Both synthesized entries carry the same count fields the server's own entries do, so
     formatPlanCounts() can describe any of the eight values without asking which kind of
     entry it was handed. A label that has no bucket at all (a pin whose members have gone)
     reports zeros rather than omitting the fields. */
  const fromBucket = (key, b) => ({
    key, label: key,
    count: b ? b.count : 0,
    rank_count: b ? b.rank_count : 0,
    pass_count: b ? b.pass_count : 0,
    warn_count: b ? b.warn_count : 0,
  });
  if (strategy === 'manual') {
    if (!pin) return null;
    return fromBucket(pin, bucketFor(pin));
  }
  if (strategy === 'highest_score') {
    if (!derived) return { key: null, label: null, count: 0, rank_count: 0,
                           pass_count: 0, warn_count: 0 };
    return fromBucket(derived, bucketFor(derived));
  }
  return (plan && plan.strategies && plan.strategies[strategy]) || null;
}

/* The one-line consequence under the picker: how many members this choice leaves
   eligible, and what happens to the rest. It says "nothing is turned off" out loud
   because that is the sixth pass's whole correction - the lock filters where members
   are chosen and never mutates a participation switch (§4.1).

   `measuredCount` is how many members have a measured format at all, and it is what
   keeps the consequence clause honest: an UNTESTED member is never filtered out
   (unknown is not proven-different - CLAUDE.md "Format lock filters, health score
   ranks"), so counting the whole remainder as "will be skipped" tells the user their
   feeds are being dropped when every one of them is still eligible. On a group with 24
   members and 2 tested, the old wording claimed 22 would be skipped; the real answer is
   zero. Omit it and the clause stays deliberately vague rather than stating a number
   this function cannot compute. */
function formatPlanSummary(plan, strategy, pin, derived, memberCount, measuredCount) {
  /* When the server ranked over a narrower population than the members it was handed -
     the group's recording-enabled ones - the consequence line describes THAT population,
     because it is the one the lock actually filters. Saying "records from 26 of 105
     members" on a group where 76 of them are health-check-only describes a group the user
     does not have. The caller's own counts still win when nothing was narrowed, which is
     the create/clone preview: there `memberCount` is the kept set the user is editing
     right now and the server has never seen it (dev/changelog/890). */
  const narrowed = !!(plan && plan.rank_total != null && plan.rank_total !== plan.total);
  const total = narrowed ? plan.rank_total : (memberCount || 0);
  const measured = narrowed ? plan.rank_measured : measuredCount;
  // Pluralized on the head noun, not on the tail: "member set to records" is what
  // appending an s to the whole phrase produces.
  const members = (n) => (narrowed ? `member${n === 1 ? '' : 's'} set to record`
                                   : `member${n === 1 ? '' : 's'}`);
  if (strategy === 'health_check_only') {
    return 'This group is tested and nothing else. It cannot be added to the TV Guide and no ' +
      'recording will run from it until you choose one of the other strategies.';
  }
  if (strategy === 'unmanaged') {
    return `No format is enforced, so all ${total} ${members(total)} stay eligible ` +
      'whatever they measure. A failover between two formats produces one file whose format ' +
      'changes partway through.';
  }
  const entry = formatPlanEntry(plan, strategy, pin, derived);
  if (!entry || !entry.key) {
    if (strategy === 'manual') {
      return 'No format has been measured on this group yet, so there is nothing to pin. Run a ' +
        'health check first, or choose a strategy that follows the data.';
    }
    // The server names the specific reason - no healthy formats, none the group records
    // from, or nothing broad enough for Balanced - and those take three different actions
    // to fix. Restating one sentence for all three here would throw that away.
    return (plan && plan.strategies && plan.strategies[strategy]
      && plan.strategies[strategy].rationale)
      || 'No format has enough healthy channels to build a group on.';
  }
  // The matching count from the same population `total` counts, or the two halves of the
  // sentence describe different groups.
  const matched = narrowed ? (entry.rank_count || 0) : entry.count;
  const nonMatch = Math.max(0, total - matched);
  const head = `Records from ${matched} of ${total} ${members(total)}.`;
  if (nonMatch <= 0) return head;
  if (measured == null) {
    return `${head} The others are skipped when a recording picks a member only where ` +
      'their measured format differs - a member that has never been tested stays ' +
      'eligible. Nothing is turned off.';
  }
  // Split the remainder into the two groups it is actually made of. They behave
  // differently, so one number covering both is the number nobody can explain.
  const skipped = Math.max(0, Math.min(measured, total) - matched);
  const untested = Math.max(0, total - Math.min(measured, total));
  const parts = [];
  if (skipped > 0) {
    parts.push(`${skipped} measure${skipped === 1 ? 's' : ''} a different format and ` +
      `${skipped === 1 ? 'is' : 'are'} skipped when a recording picks a member`);
  }
  if (untested > 0) {
    parts.push(`${untested} ${untested === 1 ? 'has' : 'have'} never been tested and ` +
      `${untested === 1 ? 'stays' : 'stay'} eligible - unknown is not a mismatch`);
  }
  return `${head} ${parts.join('; ')}. Nothing is turned off.`;
}

/* The pinned-format picker for `manual`. A pin already stored is offered even when no
   member measures it any more - a format stays pinned after the member that justified
   it changed or left the group, and dropping it from the list would silently unpin it. */
function formatPlanPinSelect(id, plan, pin) {
  const labels = ((plan && plan.buckets) || []).map((b) => b.label);
  if (pin && labels.indexOf(pin) === -1) labels.unshift(pin);
  if (!labels.length) {
    return `<select id="${escHtml(id)}" disabled><option>No format measured yet</option></select>`;
  }
  return `<select id="${escHtml(id)}">` + labels.map((k) =>
    `<option value="${escHtml(k)}"${k === pin ? ' selected' : ''}>${escHtml(k)}</option>`
  ).join('') + '</select>';
}

/* The full bucket table - every format the health check measured, channel count,
   median bitrate, bits/pixel/frame (informational only, never ranked on), the winning
   bucket for `strategy` marked. table.tbl inside table-scroll, matching the app's one
   table system.

   `pin`/`derived` are formatPlanEntry's, so the winner marker moves for the four values
   the server's `strategies` map does not answer for as well as for the four it does.
   The marker matches on the bucket LABEL rather than on resolution+fps, because an entry
   for manual or highest_score is identified by the label alone - it never came from a
   bucket, so it has no resolution/fps fields to compare. */
function formatPlanTable(plan, strategy, pin, derived) {
  if (!plan || !plan.buckets || !plan.buckets.length) return '';
  const entry = formatPlanEntry(plan, strategy, pin, derived);
  const rows = plan.buckets.slice()
    // Recordable count first: a format the group cannot record from is never a candidate,
    // so it belongs below the ones that are rather than above them on raw membership.
    .sort((a, b) => (b.rank_count - a.rank_count) || (b.count - a.count))
    .map((b) => {
      const isWinner = !!(entry && entry.key && b.label === entry.label);
      const bitrate = b.median_bitrate_kbps == null ? '--' : `${(b.median_bitrate_kbps / 1000).toFixed(2)} MB/s`;
      const bpp = b.median_bpp == null ? '--' : b.median_bpp.toFixed(3);
      const label = isWinner
        ? `<strong>${escHtml(b.label)}</strong> <span class="pr-flag pr-picked">winner</span>`
        : escHtml(b.label);
      // A bucket holding none of the members the group records from cannot win, whatever
      // its bitrate or size. Saying so on the row is what makes the winner explicable -
      // "40 channels lost to 26" reads as a bug until the 0 next to it is visible.
      const noCandidates = b.rank_count === 0;
      const cls = [isWinner ? 'fp-winner' : '', noCandidates ? 'fp-unrankable' : '']
        .filter(Boolean).join(' ');
      const recording = noCandidates
        ? '<span data-tip="No member the group records from measures this format, so it ' +
          'cannot be chosen.">0</span>'
        : String(b.rank_count);
      return `<tr${cls ? ` class="${cls}"` : ''}><td>${label}</td><td>${b.count}</td>` +
        `<td>${recording}</td><td>${formatPlanStatus(b)}</td>` +
        `<td>${bitrate}</td><td>${bpp}</td></tr>`;
    }).join('');
  return '<div class="table-scroll"><table class="tbl"><thead><tr>' +
    '<th>Format</th><th>Channels</th><th>Recording</th><th>Last test</th>' +
    '<th>Median bitrate</th><th>Bits per pixel</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table></div>';
}

/* A bucket's healthy channels split by test label. WARN counts as healthy when buckets
   are built (app/channel_groups.py::HEALTHY_TEST_LABELS) and that is deliberate - a
   diagnostic defect must not be able to move a real lock - so the honest move is to show
   the split rather than quietly drop the warning channels (dev/changelog/890). */
function formatPlanStatus(b) {
  const pass = b.pass_count || 0;
  const warn = b.warn_count || 0;
  if (!warn) return `${pass} pass`;
  if (!pass) return `<span class="fp-all-warn">${warn} warn</span>`;
  return `${pass} pass, ${warn} warn`;
}
