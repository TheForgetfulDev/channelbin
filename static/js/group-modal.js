/* Shared "Group Channels" modal for manual channel grouping.
   Used by static/js/channel-search.js's "adding to a group" context, the group detail
   page's per-row "Group with duplicates" and its "+ Add Matching Channels", and
   channels/groups.html ("Suggest members"). The channel search's own CREATE path is no
   longer one of them - that is the three-screen group-create flow now
   (static/js/group-create-flow.js, dev/changelog/831), which is also where the
   create-then-offer-a-health-check chain this file used to carry went.
   It is also where two pieces the group-create flow draws with live - `groupWarningsHtml`
   and `cgPickedHtml` - because of the two files this is the one loaded on BOTH the channel
   search and the group detail page, and a second copy of either is two surfaces that can
   come to disagree about the same thing.

   Depends on util.js (escHtml, jsonFetch, showToast, buildModal) and dup-modal.js (openDupModal). */

/**
 * opts:
 *   channels   - [{channel_id, channel_name, account_color?, account_name?}, ...] initial
 *                selection (may be empty when fixedGroup is set - suggestions supply the
 *                candidates)
 *   fixedGroup - {id, name}; locks the modal to "add members to this group"
 *                (no mode radio) and seeds suggestions from the group
 *   onDone()   - called after a successful create/add (default: reload the page)
 */
/* A member's format is its latest test's resolution + fps. No test, no format -
   "untested", never a guess. Same rule as app/channel_groups.py's own. */
function cgShortFormat(fmt) {
  if (!fmt || !fmt.resolution) return null;
  const h = String(fmt.resolution).split('x')[1];
  return h ? `${h}p${fmt.fps ? Math.round(fmt.fps) : ''}` : String(fmt.resolution);
}

/* The picked channels, drawn as a LIST. What this replaced, on every caller, was a row of
   `.badge b-abort` pills: the visual language of a removable filter chip, on something
   inert with no click handler behind it, in a colour the badge scale reserves for a
   status. It lives here rather than in the flow that first drew it because this file is
   the one of the two loaded on both the channel search and the group detail page
   (dev/changelog/832).

   `formatFor` returns a channel's measured format or null. `known` is false while the
   fetch is still out, and the meta cell then says nothing at all: "untested" is a claim
   about the channel, and a request that has not answered yet is not evidence for it. A
   caller with no format data at all passes `known` false and never says anything. */
function cgPickedHtml(rows, formatFor, known) {
  return `<div class="cg-picked">${rows.map((r) => {
    const f = cgShortFormat(formatFor(r));
    const dot = r.account_color
      ? `<span class="color-dot" style="background:${escHtml(r.account_color)}"${
        r.account_name ? ` data-tip="${escHtml(r.account_name)}"` : ''}></span>`
      : '';
    const meta = f ? escHtml(f) : (known ? 'untested' : '');
    return `<div class="cg-picked-row">${dot}`
      + `<span class="cg-picked-name">${escHtml(r.channel_name)}</span>`
      + `<span class="cg-picked-meta">${meta}</span></div>`;
  }).join('')}</div>`;
}

/* The soft warnings POST /api/channel-groups and .../members answer with when they refuse
   to proceed unforced (`{success: false, format_mismatch, unverified_format,
   duplicate_warning}`), rendered as prose plus a Proceed anyway. Declared at file top
   level so the group-create flow draws the identical warnings from the identical response
   rather than paraphrasing them a second time (dev/changelog/831).

   `forceId` is the id to put on the Proceed anyway button - the caller owns the click,
   because what "proceed" re-submits is the caller's own request. `dedupId`, when given,
   adds the "Choose Which to Keep…" button for a caller that can host openDupModal. */
function groupWarningsHtml(data, forceId, dedupId) {
  let html = '';
  if (data.format_mismatch) {
    // A warning with a Proceed anyway, never a refusal: a member whose format does not
    // match is skipped where members are chosen and stays exactly as the user left it, so
    // the group is a legal setup that heals itself if the member starts matching again
    // (dev/changelog/762). Only a group that records asks this at all.
    const fmtList = (data.format_mismatch.groups || []).map((g) => {
      const names = g.channels.map((c) => escHtml(c.channel_name)).join(', ');
      return `<li><strong>${escHtml(g.format)}</strong> - ${names}</li>`;
    }).join('');
    const untested = (data.format_mismatch.untested || []).map((c) => escHtml(c.channel_name)).join(', ');
    html += '<div style="margin-bottom:0.5rem;">' +
      '<strong style="color:var(--warn);">⚠ Mixed resolution / frame rate</strong><br>' +
      '<span class="text-muted small">These feeds are not all the same format. This group ' +
      'records, so its group format decides which members may be picked - the ones that do ' +
      'not match stay in the group, stay switched on, and are skipped until they match ' +
      'again. Within one recording, failover sticks to the format it started on.</span>' +
      `<ul class="small" style="margin:0.35rem 0 0; padding-left:1.25rem;">${fmtList}</ul>` +
      (untested ? '<div class="text-muted small" style="margin-top:0.35rem;">' +
        `Not checked (no health data): ${untested}</div>` : '') +
      '</div>';
  }
  if (data.unverified_format) {
    const names = data.unverified_format.map((c) => escHtml(c.channel_name)).join(', ');
    html += `<div style="margin-bottom:0.5rem;"><strong style="color:var(--warn);">⚠ Format not verified</strong><br>
      <span style="font-size:12px; color:var(--text-muted);">${names} - no health data yet, so we can't
      confirm the resolution/frame rate matches this group. Run a health check to verify; if it later
      proves different, you'll be warned to remove it.</span></div>`;
  }
  if (data.duplicate_warning) {
    html += `<div style="margin-bottom:0.5rem;"><strong style="color:var(--warn);">⚠ Duplicate stream URLs</strong><br>
      <span style="font-size:12px; color:var(--text-muted);">Some of these channels point at the <em>same</em> stream URL -
      they add no failover redundancy (if one feed dies, so do the others).</span></div>`;
  }
  html += '<div style="display:flex; gap:0.5rem; flex-wrap:wrap;">';
  if (data.duplicate_warning && dedupId) {
    html += `<button type="button" class="btn btn-sm" id="${dedupId}">Choose Which to Keep…</button>`;
  }
  html += `<button type="button" class="btn btn-sm btn-danger-outline" id="${forceId}">Proceed Anyway</button></div>`;
  return html;
}

function openGroupModal(opts) {
  const initial = (opts.channels || []).slice();
  const fixedGroup = opts.fixedGroup || null;
  const onDone = opts.onDone || (() => location.reload());
  if (!initial.length && !fixedGroup) return;

  const existing = document.getElementById('group-modal');
  if (existing) existing.remove();

  // Name prefill: words (case-insensitive) common to every selected channel name,
  // in first-name order; falls back to the first channel's name.
  const prefillName = () => {
    if (!initial.length) return '';
    const wordSets = initial.map(c =>
      new Set(c.channel_name.toLowerCase().split(/\s+/).filter(Boolean)));
    const common = initial[0].channel_name.split(/\s+/).filter(w =>
      wordSets.every(s => s.has(w.toLowerCase())));
    return (common.length ? common.join(' ') : initial[0].channel_name).trim();
  };

  /* This modal knows nothing about any channel's measured format - its callers hand it a
     selection, not health data - so `known` is false and the meta cell stays empty rather
     than claiming "untested" about channels nobody asked about. */
  const pickedHtml = cgPickedHtml(initial, () => null, false);

  const modeHtml = fixedGroup ? '' : `
    <div class="ext-scope-label" style="display:flex; flex-direction:column; gap:0.25rem; margin-bottom:0.75rem;">
      <label style="display:flex; align-items:center; gap:0.4rem;">
        <input type="radio" name="group-mode" value="new" checked> Create new group
      </label>
      <label style="display:flex; align-items:center; gap:0.4rem;">
        <input type="radio" name="group-mode" value="existing" id="group-mode-existing" disabled>
        Add to existing group
        <select id="group-existing-select" class="form-control" style="max-width:220px; display:inline-block;" disabled></select>
      </label>
    </div>`;

  const body = document.createElement('div');
  body.innerHTML = `
    <div id="group-modal-error" style="display:none; color:var(--bad); margin-bottom:0.75rem; font-size:0.875rem;"></div>
    ${initial.length ? `
    <div class="form-group">
      <div class="gd-field-lbl">Channels selected (${initial.length})</div>
      ${pickedHtml}
    </div>` : ''}
    ${modeHtml}
    ${fixedGroup ? '' : `
    <div class="form-group" id="group-name-wrap">
      <label style="font-size:12px; margin-bottom:2px; display:block;">Group Name</label>
      <input type="text" id="group-name-input" class="form-control" value="${escHtml(prefillName())}" maxlength="255">
    </div>`}
    <div class="modal-loading" id="group-suggest-loading" style="display:none" role="status" aria-live="polite">
      <span class="spinner modal-loading-spin" aria-hidden="true"></span>
      <span>Looking for channels to add...</span>
    </div>
    <div id="group-suggest-empty" class="text-muted small" style="display:none; margin-bottom:0.75rem;"></div>
    <div id="group-suggest-wrap" style="display:none; margin-bottom:0.75rem;">
      <div id="group-suggest-note" style="font-size:12px; color:var(--text-muted); margin-bottom:0.25rem;">
        Suggested duplicates (same EPG ID or matching name) - check to include:
      </div>
      <div id="group-suggest-list" style="max-height:240px; overflow-y:auto; border:1px solid var(--border); border-radius:var(--radius-sm); padding:0.5rem;"></div>
      <div id="group-suggest-excluded" style="font-size:12px; color:var(--text-muted); margin-top:0.25rem; display:none;"></div>
    </div>
    <div id="group-warning-wrap" style="display:none; border:1px solid var(--warn); border-radius:var(--radius-sm); padding:0.75rem; margin-bottom:0.75rem;"></div>`;

  let submitBtn = null;

  const modal = buildModal({
    title: fixedGroup ? `Add Channels - ${fixedGroup.name}` : 'Group Channels',
    body,
    footer: [
      { label: 'Cancel', class: 'btn' },
      { label: fixedGroup ? 'Add Channels' : 'Create Group', class: 'btn btn-primary',
        onClick: () => { submit(false); return false; } },
    ],
  });
  modal.id = 'group-modal';
  submitBtn = modal.querySelector('.modal-foot .btn-primary');
  const panelEl = modal.querySelector('.modal-panel');

  const errEl = document.getElementById('group-modal-error');
  const showErr = (msg) => { errEl.textContent = msg; errEl.style.display = ''; };

  // ── Existing-groups dropdown ─────────────────────────────────────────────
  if (!fixedGroup) {
    jsonFetch('/api/channel-groups').then(data => {
      const groups = data.groups || [];
      if (!groups.length) return;
      const sel = document.getElementById('group-existing-select');
      sel.innerHTML = groups.map(g =>
        `<option value="${g.id}">${escHtml(g.name)} (${g.member_count})</option>`).join('');
      sel.disabled = false;
      document.getElementById('group-mode-existing').disabled = false;
    }).catch(() => { /* dropdown stays disabled; creating still works */ });

    body.querySelectorAll('input[name="group-mode"]').forEach(r => {
      r.addEventListener('change', () => {
        const isNew = body.querySelector('input[name="group-mode"]:checked').value === 'new';
        document.getElementById('group-name-wrap').style.display = isNew ? '' : 'none';
        submitBtn.textContent = isNew ? 'Create Group' : 'Add to Group';
      });
    });
  }

  // ── Suggestions (single-channel seed, or the fixed group) ────────────────
  // Known format-mismatch candidates are rendered non-selectable; keep their ids so a
  // submit can never include one even if a disabled checkbox is forced checked via DOM.
  const nonSelectableIds = new Set();
  const suggestUrl = fixedGroup
    ? `/api/channel-groups/suggest?group_id=${fixedGroup.id}`
    : (initial.length === 1 ? `/api/channel-groups/suggest?channel_id=${initial[0].channel_id}` : null);

  /* The fixed-group path is the only one that can open on an EMPTY body - the group page's
     "+ Add Matching Channels" passes no selection and no name field is drawn, so until this
     fetch answers there is nothing on screen and nothing to submit. It used to show exactly
     that, with both footer buttons live: indistinguishable from a modal that had finished
     and found nothing, and an Add Channels whose only possible answer was "Select at least
     one channel" - blaming the user for the app still working.

     Cancel deliberately stays live throughout. Nothing has been written at this point, so
     leaving is always safe, and a fetch that hangs without ever rejecting would otherwise
     leave the modal with no usable footer at all (dev/changelog/832).

     One updater owns the loading row, #group-suggest-empty and #group-suggest-wrap: this
     settle() and the render below it, never a second path. */
  let suggestSettled = false;
  const settleSuggest = (emptyMsg, gotResults) => {
    if (suggestSettled) return;
    suggestSettled = true;
    document.getElementById('group-suggest-loading').style.display = 'none';
    /* Give the button back only once something exists for it to send. With no incoming
       selection AND no suggestions this modal holds nothing at all, so an enabled Add
       Channels could still only produce "Select at least one channel" - the message
       beside it is the honest answer instead. */
    if (submitBtn.disabled && (initial.length || gotResults)) submitBtn.disabled = false;
    if (emptyMsg) {
      const el = document.getElementById('group-suggest-empty');
      el.textContent = emptyMsg;
      el.style.display = '';
    }
  };

  if (fixedGroup) {
    document.getElementById('group-suggest-loading').style.display = '';
    submitBtn.disabled = true;
  }
  if (suggestUrl) {
    jsonFetch(suggestUrl).then(data => {
      const results = data.results || [];
      if (!results.length) {
        /* Only the fixed-group path says so: on the single-channel seed the suggestions
           are an offer on top of a modal that already holds its selection and its name
           field, and "we found nothing extra" is not news there. */
        settleSuggest(fixedGroup
          ? `Nothing else looks like a member of "${fixedGroup.name}" - no other channel `
            + 'matched it by EPG ID or by name.'
          : null, false);
        return;
      }
      const reasonLabel = { 'epg_id+name': 'EPG + name', 'epg_id': 'EPG ID', 'name': 'name' };
      // Format badge: confirmed = same resolution/FPS as the group reference (recommended
      // pick); unverified = no health data, format can't be confirmed (added on faith,
      // subject to the post-test re-check); different = tested and a proven format mismatch
      // (shown for visibility but not addable - the create/add guard would hard-block it).
      const fmtBadge = (r) => {
        if (r.format_status === 'confirmed')
          return `<span class="badge b-done" title="${escHtml(r.format || '')}">✓ same format</span>`;
        if (r.format_status === 'different')
          return `<span class="badge b-fail" title="${escHtml(r.format || '')} - different resolution/frame rate">✗ mismatch</span>`;
        return `<span class="badge b-warn" title="no health data - run a health check to confirm">⚠ unverified</span>`;
      };
      results.forEach(r => { if (r.selectable === false) nonSelectableIds.add(Number(r.channel_id)); });
      // Stat cells come from the candidate's latest health test; unknowns render as "-".
      const numCell = (v) => (v === null || v === undefined || v === '') ? '-' : v;
      const brCell = (kbps) => (kbps === null || kbps === undefined) ? '-' : `${(kbps / 1000).toFixed(2)} MB/s`;
      const scoreCell = (v) => (v === null || v === undefined) ? '-' : `★${Math.round(v)}`;
      const rowsHtml = results.map(r => {
        const disabled = r.selectable === false;
        const curGroups = r.current_groups || [];
        const groupNote = curGroups.length === 1
          ? `in group "${escHtml(curGroups[0])}"`
          : (curGroups.length > 1 ? `in ${curGroups.length} groups (${curGroups.map(escHtml).join(', ')})` : '');
        const meta = [escHtml(r.account_name), reasonLabel[r.reason] || r.reason]
          .concat(groupNote ? [groupNote] : [])
          .concat(r.in_guide ? ['in guide'] : [])
          .join(' · ');
        return `
        <tr class="${disabled ? 'suggest-row-disabled' : ''}">
          <td class="st-cb"><input type="checkbox" class="group-suggest-cb" value="${r.channel_id}"
                 data-channel-name="${escHtml(r.channel_name)}"${disabled ? ' disabled' : ''}></td>
          <td class="st-fmt">${fmtBadge(r)}</td>
          <td class="st-ch">
            <span class="account-dot" style="background:${escHtml(r.account_color || '#30363d')}" title="${escHtml(r.account_name)}"></span>
            <a href="/channels/${r.channel_id}" target="_blank" rel="noopener" class="channel-name-link">${escHtml(r.channel_name)}</a>
            <div class="st-meta">${meta}</div>
          </td>
          <td class="st-num">${numCell(r.resolution)}</td>
          <td class="st-num">${numCell(r.fps)}</td>
          <td class="st-num">${brCell(r.bitrate_kbps)}</td>
          <td class="st-num">${scoreCell(r.health_score)}</td>
        </tr>`;
      }).join('');
      document.getElementById('group-suggest-list').innerHTML = `
        <div class="table-scroll">
          <table class="suggest-table">
            <thead><tr>
              <th class="st-cb"></th><th class="st-fmt">Format</th><th class="st-ch">Channel</th>
              <th class="st-num">Res</th><th class="st-num">FPS</th>
              <th class="st-num">Bitrate</th><th class="st-num">Score</th>
            </tr></thead>
            <tbody>${rowsHtml}</tbody>
          </table>
        </div>`;
      // Widen the modal for the stats table (safe on mobile - modal-panel is width:100%).
      panelEl.style.maxWidth = '900px';
      // Header copy: explain what "suggested" means and how format is confirmed. When the
      // seed itself is untested, no candidate can be confirmed - say so once for the list.
      document.getElementById('group-suggest-note').innerHTML = data.seed_format_known
        ? `Matched by EPG ID / name; <strong>✓ same format</strong> is confirmed against this
           channel's ${escHtml(data.seed_format)} health data so grouped feeds stay one format for
           clean failover. <strong>⚠ unverified</strong> feeds have no health data yet.`
        : `Matched by EPG ID / name. This channel has no health data, so resolution/frame-rate
           couldn't be confirmed for any suggestion - run a health check to verify before grouping.`;
      if (data.different_count) {
        const excl = document.getElementById('group-suggest-excluded');
        excl.textContent = `${data.different_count} feed(s) shown but cannot be added - different resolution/frame rate.`;
        excl.style.display = '';
      }
      settleSuggest(null, true);
      document.getElementById('group-suggest-wrap').style.display = '';
    }).catch((e) => {
      /* Suggestions stay best-effort on the single-channel seed - the modal is fully
         usable without them. On the fixed group they are the entire contents, so a
         swallowed rejection is an empty modal that never explains itself. */
      settleSuggest(null, false);
      if (fixedGroup) showErr(`Couldn't look for channels to add: ${e.message || 'request failed'}. `
                              + 'Close this and try again, or add channels from the Browse tab.');
    });
  }

  // ── Submit + warning handling ────────────────────────────────────────────
  let selectedIds = initial.map(c => Number(c.channel_id));

  const currentChannelIds = () => {
    const suggested = Array.from(body.querySelectorAll('.group-suggest-cb:checked'))
      .map(cb => Number(cb.value));
    return Array.from(new Set(selectedIds.concat(suggested)))
      .filter(id => !nonSelectableIds.has(id));
  };

  const submit = async (force) => {
    const channelIds = currentChannelIds();
    if (!channelIds.length) { showErr('Select at least one channel.'); return; }

    let url, body2;
    const mode = fixedGroup ? 'fixed'
      : body.querySelector('input[name="group-mode"]:checked').value;
    if (mode === 'fixed') {
      url = `/api/channel-groups/${fixedGroup.id}/members`;
      body2 = { channel_ids: channelIds, force };
    } else if (mode === 'existing') {
      const gid = document.getElementById('group-existing-select').value;
      if (!gid) { showErr('Pick a group to add to.'); return; }
      url = `/api/channel-groups/${gid}/members`;
      body2 = { channel_ids: channelIds, force };
    } else {
      const name = document.getElementById('group-name-input').value.trim();
      if (!name) { showErr('Group name is required.'); return; }
      url = '/api/channel-groups';
      body2 = { name, channel_ids: channelIds, force };
    }

    submitBtn.disabled = true;
    errEl.style.display = 'none';
    try {
      const data = await jsonFetch(url, { method: 'POST', body: JSON.stringify(body2) });
      if (data.success) {
        modal.closeModal();
        showToast(fixedGroup || mode === 'existing'
          ? `Added ${channelIds.length} channel(s) to "${data.group_name}".`
          : `Group "${data.group_name}" created.`);
        onDone();
        return;
      }
      renderWarnings(data, channelIds);
    } catch (e) {
      showErr(e.message || 'Request failed.');
    } finally {
      submitBtn.disabled = false;
    }
  };

  const renderWarnings = (data, channelIds) => {
    const wrap = document.getElementById('group-warning-wrap');
    wrap.style.borderColor = 'var(--warn)';
    wrap.innerHTML = groupWarningsHtml(data, 'group-warn-force', 'group-warn-dedup');
    wrap.style.display = '';

    document.getElementById('group-warn-force').addEventListener('click', () => {
      wrap.style.display = 'none';
      submit(true);
    });
    const dedupBtn = document.getElementById('group-warn-dedup');
    if (dedupBtn) {
      dedupBtn.addEventListener('click', () => {
        openDupModal({
          groups: data.dup_groups,
          showGuideChoice: false,
          showTransfer: false,
          submitLabel: 'Keep Selected',
          introHtml: 'The channels you don\'t keep are dropped from <strong>this selection</strong> ' +
                     'before the group is created. Nothing is deleted or changed until you submit.',
          onSubmit: (removals) => {
            // "Remove" here means excluded from the group selection only.
            const losers = new Set(removals.map(r => r.channel_id));
            selectedIds = channelIds.filter(id => !losers.has(id));
            body.querySelectorAll('.group-suggest-cb:checked').forEach(cb => {
              if (losers.has(Number(cb.value))) cb.checked = false;
            });
            wrap.style.display = 'none';
            submit(true);
          },
        });
      });
    }
  };
}
