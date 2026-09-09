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

function fetchFormatPlan(groupId, jobId) {
  const qs = jobId ? `?job_id=${encodeURIComponent(jobId)}` : '';
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
  return `${label} - ${entry.label} (${entry.count} channel${entry.count === 1 ? '' : 's'})`;
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
  if (strategy === 'manual') {
    if (!pin) return null;
    const b = bucketFor(pin);
    return { key: pin, label: pin, count: b ? b.count : 0 };
  }
  if (strategy === 'highest_score') {
    if (!derived) return { key: null, label: null, count: 0 };
    const b = bucketFor(derived);
    return { key: derived, label: derived, count: b ? b.count : 0 };
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
  const total = memberCount || 0;
  if (strategy === 'health_check_only') {
    return 'This group is tested and nothing else. It cannot be added to the TV Guide and no ' +
      'recording will run from it until you choose one of the other strategies.';
  }
  if (strategy === 'unmanaged') {
    return `No format is enforced, so all ${total} member${total === 1 ? '' : 's'} stay eligible ` +
      'whatever they measure. A failover between two formats produces one file whose format ' +
      'changes partway through.';
  }
  const entry = formatPlanEntry(plan, strategy, pin, derived);
  if (!entry || !entry.key) {
    return strategy === 'manual'
      ? 'No format has been measured on this group yet, so there is nothing to pin. Run a health ' +
        'check first, or choose a strategy that follows the data.'
      : 'No format has enough healthy channels to build a group on.';
  }
  const nonMatch = Math.max(0, total - entry.count);
  const head = `Records from ${entry.count} of ${total} member${total === 1 ? '' : 's'}.`;
  if (nonMatch <= 0) return head;
  if (measuredCount == null) {
    return `${head} The others are skipped when a recording picks a member only where ` +
      'their measured format differs - a member that has never been tested stays ' +
      'eligible. Nothing is turned off.';
  }
  // Split the remainder into the two groups it is actually made of. They behave
  // differently, so one number covering both is the number nobody can explain.
  const skipped = Math.max(0, Math.min(measuredCount, total) - entry.count);
  const untested = Math.max(0, total - Math.min(measuredCount, total));
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
    .sort((a, b) => b.count - a.count)
    .map((b) => {
      const isWinner = !!(entry && entry.key && b.label === entry.label);
      const bitrate = b.median_bitrate_kbps == null ? '--' : `${(b.median_bitrate_kbps / 1000).toFixed(2)} MB/s`;
      const bpp = b.median_bpp == null ? '--' : b.median_bpp.toFixed(3);
      const label = isWinner
        ? `<strong>${escHtml(b.label)}</strong> <span class="pr-flag pr-picked">winner</span>`
        : escHtml(b.label);
      return `<tr${isWinner ? ' class="fp-winner"' : ''}><td>${label}</td><td>${b.count}</td>` +
        `<td>${bitrate}</td><td>${bpp}</td></tr>`;
    }).join('');
  return '<div class="table-scroll"><table class="tbl"><thead><tr>' +
    '<th>Format</th><th>Channels</th><th>Median bitrate</th><th>Bits per pixel</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table></div>';
}
