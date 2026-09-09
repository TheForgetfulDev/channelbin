/* Shared "Remove Duplicates" modal for duplicate-channel-set cleanup.
   Call sites: templates/channels/health_check_detail.html (health-check job scope),
   static/js/channel-search.js (the Browse tab, TV Guide scope),
   templates/channels/group_detail.html
   and static/js/group-modal.js (channel-group member scope).

   Every caller MUST pass introHtml - what "remove" actually means differs per surface
   (this health check / your guide / this group), and getting that wrong is the exact
   confusion this modal was reworked to fix. Depends on util.js (escHtml, buildModal). */

/**
 * opts:
 *   groups           - [{suggested_keep_id, channels: [{channel_id, channel_name,
 *                        account_name, in_guide, category_name, score, disabled,
 *                        is_oldest, test_status, tested_et, lifecycle, lifecycle_date}, ...]}, ...]
 *                       (per-channel fields beyond name/account/in_guide are optional -
 *                        only the health-check callers hand over test state)
 *   introHtml        - required; describes what removal does on THIS surface
 *   title            - optional heading override
 *   submitLabel      - optional submit-button override
 *   showGuideChoice  - true on the health-check page (job-only vs job+guide);
 *                       false elsewhere (removal always just means "remove from guide")
 *   showTransfer     - default true; set false where there is nothing to transfer yet
 *                       (the Create-group modal's pre-creation duplicate warning - the
 *                       channels aren't members of anything until the group is created)
 *   onSubmit(removals, transfer) - async fn; removals = [{channel_id, remove_from_guide,
 *                         keep_channel_id}, ...] (non-kept channels only); transfer is the
 *                         "transfer state to the kept channel" checkbox state (always
 *                         false when showTransfer is false). Resolve with {error} to show
 *                         an inline error and keep the modal open; resolve with anything
 *                         else (or nothing) to close it.
 */
function openDupModal(opts) {
  const groups = opts.groups || [];
  if (!groups.length) return;

  const anyLoserInGuide = !!opts.showGuideChoice && groups.some(g => {
    const keepId = g.suggested_keep_id;
    return g.channels.some(ch => ch.in_guide && ch.channel_id !== keepId);
  });

  const existing = document.getElementById('dup-modal');
  if (existing) existing.remove();

  const badge = (label, cls) => `<span class="badge ${cls}">${label}</span>`;

  const rowMeta = (ch) => {
    const bits = [];
    if (ch.category_name) bits.push(escHtml(ch.category_name));
    if (typeof ch.score === 'number') {
      // Only annotate a *known* zero - an absent count means the caller didn't send one,
      // which is not the same as "never observed".
      bits.push(`score ${ch.score}${ch.health_score_sample_count === 0 ? ' (unrated)' : ''}`);
    }
    if (ch.test_status && ch.test_status !== 'WAITING') {
      bits.push(`last test ${ch.test_status}${ch.tested_et ? ' · ' + escHtml(ch.tested_et) : ''}`);
    } else if (ch.test_status === 'WAITING') {
      bits.push('never tested');
    }
    if (!bits.length) return '';
    return `<div class="dup-row-meta">${bits.join(' · ')}</div>`;
  };

  const setsHtml = groups.map((group, gi) => {
    const channels = group.channels || [];
    const keepId = group.suggested_keep_id != null
      ? group.suggested_keep_id
      : (channels[0] || {}).channel_id;
    const rows = channels.map(ch => {
      const badges = [
        ch.in_guide ? badge('IN GUIDE', 'b-done') : '',
        ch.lifecycle === 'missing' ? badge(`MISSING ${escHtml(ch.lifecycle_date || '')}`, 'b-warn') : '',
        ch.is_oldest ? badge('OLDEST', 'b-abort') : '',
        ch.disabled ? badge('DISABLED', 'b-abort') : '',
      ].join('');
      return `<label class="dup-row">
        <input type="radio" name="dup-keep-${gi}" value="${ch.channel_id}"${ch.channel_id === keepId ? ' checked' : ''}>
        <span class="dup-row-body">
          <span class="dup-row-name">${escHtml(ch.channel_name)}
            <span class="dup-row-account">(${escHtml(ch.account_name)})</span>${badges}</span>
          ${rowMeta(ch)}
        </span>
      </label>`;
    }).join('');
    return `<div class="dup-set-card">
      <div class="dup-set-title">Duplicate set ${gi + 1} of ${groups.length} - keep which one?</div>
      ${rows}
    </div>`;
  }).join('');

  const guideChoiceHtml = anyLoserInGuide
    ? `<div class="ext-scope-label" style="margin-top:0.75rem; display:flex; flex-direction:column; gap:0.25rem;">
         <label style="display:flex; align-items:center; gap:0.4rem;"><input type="radio" name="dup-scope" value="job-only" checked> Remove from health check only</label>
         <label style="display:flex; align-items:center; gap:0.4rem;"><input type="radio" name="dup-scope" value="job-and-guide"> Remove from health check <em>and</em> the TV Guide</label>
       </div>`
    : '';

  // Default true - not transferring is the bug this checkbox exists to fix (a removed
  // duplicate's guide listing/group membership/scheduled recordings/health-check
  // enrollment used to just vanish). False only where nothing exists yet to transfer.
  const showTransfer = opts.showTransfer !== false;
  const transferHtml = showTransfer
    ? `<label class="dup-transfer-label" style="margin-top:0.75rem; display:flex; align-items:center; gap:0.4rem;">
         <input type="checkbox" id="dup-transfer" checked>
         Transfer guide listing, group membership, scheduled recordings, and health-check
         enrollment from each removed channel to the one you keep
       </label>`
    : '';

  const body = document.createElement('div');
  body.innerHTML =
    '<div class="dup-modal-intro">' +
      '<p><strong>Select which channel you\'d like to keep from each duplicate set.</strong> ' +
      'Everything you don\'t select is removed.</p>' +
      `<p>${opts.introHtml || ''}</p>` +
      '<p class="dup-modal-note">These are the same underlying stream, listed more than once ' +
      'in your provider\'s channel list.</p>' +
    '</div>' +
    '<div id="dup-modal-error" style="display:none; color:var(--bad); margin-bottom:0.75rem; font-size:0.875rem;"></div>' +
    `<div id="dup-modal-sets">${setsHtml}</div>` +
    guideChoiceHtml +
    transferHtml;

  let submitBtn = null;

  function submit(close) {
    const scopeInput = body.querySelector('input[name="dup-scope"]:checked');
    const removeFromGuide = anyLoserInGuide && scopeInput && scopeInput.value === 'job-and-guide';
    const transferInput = body.querySelector('#dup-transfer');
    const transfer = showTransfer && !!transferInput && transferInput.checked;

    const removals = [];
    groups.forEach((group, gi) => {
      const channels = group.channels || [];
      const keepInput = body.querySelector(`input[name="dup-keep-${gi}"]:checked`);
      const keepId = keepInput ? Number(keepInput.value) : (channels[0] || {}).channel_id;
      channels.forEach(ch => {
        if (ch.channel_id !== keepId) {
          removals.push({ channel_id: ch.channel_id, remove_from_guide: !!removeFromGuide,
                          keep_channel_id: keepId });
        }
      });
    });

    const origText = submitBtn.textContent;
    submitBtn.disabled = true;
    submitBtn.textContent = 'Removing…';
    const errEl = document.getElementById('dup-modal-error');
    errEl.style.display = 'none';

    Promise.resolve(opts.onSubmit(removals, transfer)).then(result => {
      if (result && result.error) {
        errEl.textContent = result.error;
        errEl.style.display = '';
        submitBtn.disabled = false;
        submitBtn.textContent = origText;
        return;
      }
      close();
    }).catch(() => {
      errEl.textContent = 'Request failed. Check your connection.';
      errEl.style.display = '';
      submitBtn.disabled = false;
      submitBtn.textContent = origText;
    });
  }

  const modal = buildModal({
    title: opts.title || 'Remove Duplicate Channels',
    body,
    panelClass: 'modal-wide',
    footer: [
      { label: 'Cancel', class: 'btn' },
      { label: opts.submitLabel || 'Keep Selected & Remove Duplicates', class: 'btn btn-danger',
        onClick: (close) => { submit(close); return false; } },
    ],
  });
  modal.id = 'dup-modal';
  submitBtn = modal.querySelector('.modal-foot .btn-danger');
}
