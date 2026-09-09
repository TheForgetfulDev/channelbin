/* "Pick best format..." modal for an EXISTING channel group (item 2 of the auto-select-
   format feature; item 1 is create-group-modal.js, dev/changelog/494). Re-runs the shared
   format-selection engine (app/channel_groups.py::plan_format_selection, format-plan.js)
   over the group's CURRENT members, previews the pick, then applies it via
   POST .../apply-format-plan - which recomputes the plan server-side itself, so nothing
   this file sends is trusted as the channel list.

   Depends on util.js (escHtml, jsonFetch, showToast, buildModal, fieldRow) and
   format-plan.js (fetchFormatPlan, groupStrategyLabel, formatPlanOptionLabel,
   formatPlanTable, FORMAT_STRATEGY_KEYS), both loaded before this file.

   opts:
     groupId, groupName
     channels     - [{id, name}] for every current member, display-only (naming the
                    protected member in the post-apply summary) - never used to decide
                    what gets kept/removed, that is entirely server-side
     onDone(resp) - called on success; default reloads the page

   Deliberately fetches the plan with no job_id: the apply endpoint has no single "the"
   health check to scope an existing group's ongoing membership to (unlike item 1's
   create-group preview, which previews the one check it was opened from) - it uses each
   member's own latest test regardless of job, so this preview matches that basis exactly.
*/
function openFormatPickerModal(opts) {
  const onDone = opts.onDone || (() => location.reload());
  const nameOf = (id) => {
    const c = (opts.channels || []).find(x => x.id === id);
    return c ? c.name : `channel ${id}`;
  };

  let planData = null, planLoading = true, planError = null;
  let strategy = 'highest_bitrate';
  let nonMatching = 'keep';

  fetchFormatPlan(opts.groupId)
    .then((resp) => { planData = resp; planLoading = false; render(); })
    .catch((err) => { planError = err.message || 'failed to load'; planLoading = false; render(); });

  const body = document.createElement('div');
  let applyBtn = null;

  function currentEntry() {
    return planData && planData.strategies ? planData.strategies[strategy] : null;
  }

  function strategyControl() {
    if (planLoading) return '<select id="fp-strategy" disabled><option>Loading&hellip;</option></select>';
    if (planError) return '<select id="fp-strategy" disabled><option>Unavailable</option></select>';
    const optsHtml = FORMAT_STRATEGY_KEYS.map((key) =>
      `<option value="${key}"${strategy === key ? ' selected' : ''}>` +
      `${escHtml(formatPlanOptionLabel(planData, key))}</option>`).join('');
    return `<select id="fp-strategy">${optsHtml}</select>`;
  }

  // The bucket table plus a one-line summary. Never omitted for a no-winner strategy -
  // the rationale says why (CLAUDE.md: failure paths must be observable).
  function preview() {
    if (!planData) return '';
    const entry = currentEntry();
    const table = formatPlanTable(planData, strategy);
    let summary;
    if (!entry || !entry.key) {
      summary = entry ? entry.rationale : 'No format has enough healthy channels to pick from.';
    } else {
      const nonMatch = planData.total - entry.count;
      summary = `Keeps ${entry.count} of ${planData.total} members.` +
        (nonMatch > 0 ? ` ${nonMatch} do${nonMatch === 1 ? 'es' : ''} not match.` : '');
    }
    return `<div class="cg-auto-details">${table}<p class="text-muted">${escHtml(summary)}</p></div>`;
  }

  function render() {
    const entry = currentEntry();
    const noWinner = !planLoading && !planError && (!entry || !entry.key);
    body.innerHTML =
      '<div class="notice notice-info"><strong>Picks a video format from the current members of this ' +
        'group and locks the group to it.</strong> Members that do not match can be kept or removed - ' +
        'your choice below.</div>' +
      '<fieldset class="gd-fset"><div class="gd-fset-head">Format</div>' +
      fieldRow({ label: 'Strategy',
        meta: 'How to pick the winning format among the current members of this group.',
        control: strategyControl() }) +
      preview() +
      '</fieldset>' +
      '<fieldset class="gd-fset"><div class="gd-fset-head">Members that do not match</div>' +
      fieldRow({ full: true, label: 'What happens to them',
        meta:
          `<label class="fp-radio"><input type="radio" name="fp-nm" value="keep"${nonMatching === 'keep' ? ' checked' : ''}> ` +
          '<strong>Keep them</strong><span class="text-muted">Nothing is switched off. They stay in the group and ' +
          'are skipped whenever a recording picks a member, until they match again.</span></label>' +
          `<label class="fp-radio"><input type="radio" name="fp-nm" value="remove"${nonMatching === 'remove' ? ' checked' : ''}> ` +
          '<strong>Remove them from the group</strong><span class="text-muted">They stay in your channel list ' +
          'and can be added back by hand.</span></label>' +
          (nonMatching === 'keep'
            ? '<p class="text-muted">Members that have never been tested are never skipped by this - there is ' +
              'nothing to prove they do not match.</p>'
            : '') }) +
      '</fieldset>';
    if (applyBtn) applyBtn.disabled = noWinner;
  }

  body.addEventListener('change', (e) => {
    if (e.target.id === 'fp-strategy') { strategy = e.target.value; render(); return; }
    if (e.target.name === 'fp-nm') { nonMatching = e.target.value; render(); }
  });

  function confirmApply(onYes) {
    const entry = currentEntry();
    const n = planData.total - entry.count;
    const verb = nonMatching === 'remove' ? 'Remove them' : 'Keep them';
    const consequence = nonMatching === 'remove'
      ? `${n} member${n === 1 ? '' : 's'} will be taken out of this group. They stay in your channel list and ` +
        'can be added back by hand.'
      : `${n} member${n === 1 ? '' : 's'} will be skipped whenever a recording picks a member for this group, ` +
        `until ${n === 1 ? 'it starts' : 'they start'} matching again.`;
    buildModal({
      title: `Lock this group to ${entry.label}?`,
      panelClass: 'modal-wide',
      body: `<p><strong>The group's format will be set to ${escHtml(entry.label)}.</strong></p>` +
        `<p class="text-muted">${consequence}</p>`,
      footer: [
        { label: 'Go back', class: 'btn', onClick: (c) => c() },
        { label: verb, class: nonMatching === 'remove' ? 'btn btn-danger' : 'btn btn-primary',
          onClick: (c) => { c(); onYes(); return false; } },
      ],
    });
  }

  function apply() {
    const entry = currentEntry();
    if (!entry || !entry.key) return;
    confirmApply(() => {
      jsonFetch(`/api/channel-groups/${opts.groupId}/apply-format-plan`, {
        method: 'POST',
        body: JSON.stringify({ strategy, non_matching: nonMatching }),
      }).then((resp) => {
        let msg = `Format locked to ${resp.format.label}. Kept ${resp.kept}.`;
        if (resp.filtered) msg += ` ${resp.filtered} filtered out (still in the group).`;
        if (resp.removed) msg += ` ${resp.removed} removed.`;
        // plan_reconcile always protects the best-ranked non-manually-disabled member
        // from auto-disable so the group can never end up with none active - name it
        // when it was actually one of the non-matching members, not on every apply.
        const winningIds = new Set(entry.channel_ids);
        if (resp.protected_channel_id != null && !winningIds.has(resp.protected_channel_id)
            && (resp.disabled || resp.removed)) {
          msg += ` "${nameOf(resp.protected_channel_id)}" was kept active even though it does not match - ` +
            'it is the best-ranked member of this group, and a group can never be left with none.';
        }
        showToast(msg);
        onDone(resp);
      }).catch(err => showToast(err.message || 'Request failed.', { type: 'error' }));
    });
  }

  const modal = buildModal({
    title: `Pick best format for "${opts.groupName}"`,
    panelClass: 'modal-wide',
    body,
    footer: [
      { label: 'Cancel', class: 'btn', onClick: (c) => c() },
      { label: 'Apply', class: 'btn btn-primary', onClick: () => { apply(); return false; } },
    ],
  });
  applyBtn = modal.querySelector('.modal-foot .btn-primary');
  render();
  return modal;
}
