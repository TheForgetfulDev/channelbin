/* "Clone health check" picker - opened from a job's detail page kebab (dev/changelog -
   clone on-demand test). Lets the user narrow which of the source job's channels carry
   over into the clone before handing off to the real create-check modal.

   This is deliberately a THIN wrapper, not a rebuild of check-modal.js: it owns only the
   channel picker (checkbox list, all checked by default - CLAUDE.md "control over which
   channels carry over"), then closes itself and opens openCreateCheckModal() in its
   existing ad hoc `channelIds` mode, pre-seeded with the source job's profile and
   schedule via that modal's `initialProfileId`/`initialSchedule` opts. The source job is
   never touched - the create endpoint always makes a brand-new, independent
   group + job for an ad hoc channel list, exactly like the Browse tab's "Test selected".

   Not a reuse of create-group-modal.js's `.prune-row` picker: that grid has columns for
   format-matching concerns (reference/mismatch flags) that don't apply to cloning a
   check, and dropping columns from a shared grid class would desync its header/row
   tracks in whichever context still uses the full version (CLAUDE.md's CSS grid rule).

   Depends on util.js (escHtml, jsonFetch, showToast, buildModal) and check-modal.js
   (openCreateCheckModal).

   opts:
     jobId       - source job id, for the "run it now" style class list of channels
     jobName     - source job's name, used in the title/default name and modal copy
     channels    - [{id, name, account_name, account_color}] - the source job's channels
     profileId   - source job's profile_id (or null for global defaults)
     profiles / profilesUrl / testerBusy - forwarded to openCreateCheckModal
     schedule    - source job's schedule (same shape as G.schedule / _schedule_ctx)
     scheduleTemplateId / schedulePrefix - forwarded to openCreateCheckModal
     onDone()    - forwarded to openCreateCheckModal
*/
function openCloneCheckModal(opts) {
  const channels = opts.channels || [];
  const kept = new Set(channels.map(c => c.id));

  const body = document.createElement('div');
  let continueBtn = null;

  function render() {
    const n = kept.size;
    const rows = channels.map(c => `<label class="clone-check-row">` +
      `<input type="checkbox" data-clone-ch="${c.id}"${kept.has(c.id) ? ' checked' : ''}>` +
      `<span class="acct-dot tip-plain" data-tip="${escHtml(c.account_name || 'Account')}" ` +
        `style="background:${escHtml(c.account_color || 'var(--text-faint)')}"></span>` +
      `<span class="clone-check-name">${escHtml(c.name)}</span>` +
      `<span class="clone-check-acct">${escHtml(c.account_name || '')}</span></label>`).join('');

    body.innerHTML =
      '<div class="notice notice-info">' +
        `<strong>"${escHtml(opts.jobName)}" is not touched</strong> - this creates a brand new, ` +
        'independent health check with its own copy of the channel list, profile and schedule.' +
      '</div>' +
      '<fieldset class="gd-fset"><div class="gd-fset-head">Channels' +
        `<span class="fh-note">${n} of ${channels.length} selected</span></div>` +
      '<div class="clone-check-actions">' +
        '<button type="button" class="btn btn-sm btn-ghost" data-clone-all="1">Select all</button>' +
        '<button type="button" class="btn btn-sm btn-ghost" data-clone-all="0">Select none</button>' +
      '</div>' +
      `<div class="clone-check-list">${rows}</div></fieldset>`;

    if (continueBtn) {
      continueBtn.disabled = n === 0;
    }
  }

  body.addEventListener('change', (e) => {
    const cb = e.target.closest('[data-clone-ch]');
    if (!cb) return;
    const id = parseInt(cb.dataset.cloneCh, 10);
    if (cb.checked) kept.add(id); else kept.delete(id);
    render();
  });

  body.addEventListener('click', (e) => {
    const all = e.target.closest('[data-clone-all]');
    if (!all) return;
    if (all.dataset.cloneAll === '1') channels.forEach(c => kept.add(c.id));
    else kept.clear();
    render();
  });

  const modal = buildModal({
    title: `Clone "${opts.jobName}"`,
    panelClass: 'modal-wide',
    body,
    footer: [
      { label: 'Cancel', class: 'btn', onClick: (c) => c() },
      { label: 'Continue', class: 'btn btn-primary', onClick: (close) => {
        if (!kept.size) { showToast('Select at least one channel.', { type: 'error' }); return false; }
        close();
        openCreateCheckModal({
          channelIds: Array.from(kept),
          modalTitle: `Clone "${opts.jobName}"`,
          defaultName: `${opts.jobName} (copy)`,
          memberCount: kept.size,
          profiles: opts.profiles,
          profilesUrl: opts.profilesUrl,
          testerBusy: opts.testerBusy,
          windowSettingsUrl: opts.windowSettingsUrl,
          scheduleTemplateId: opts.scheduleTemplateId,
          schedulePrefix: opts.schedulePrefix,
          initialProfileId: opts.profileId,
          initialSchedule: opts.schedule,
          onDone: opts.onDone,
        });
        return false;
      } },
    ],
  });
  continueBtn = modal.querySelector('.modal-foot .btn-primary');
  render();
  return modal;
}
