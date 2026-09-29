/* "Schedule selected" - several showings ticked on the EPG search's airings list, one set of
   settings, each showing its own ordinary scheduled recording (dev/changelog/1157). Called
   from static/js/channel-search.js. Depends on util.js (jsonFetch, escHtml, buildModal,
   showToast, nf).

   The list is always the server's preview, fetched fresh and re-fetched when the profile
   changes, because the profile moves every padded time and so every overlap. A warning never
   stops a showing - the preview IS the confirmation, as "Proceed anyway" is in the single
   record modal - and a skip always says why. Create re-plans on the server rather than
   trusting this list, so the result is reported from what actually happened. */

/**
 * opts:
 *   items       - [{epg_id, group_id}] in the order they were ticked
 *   profiles    - [{id, name}] every recording profile
 *   previewUrl  - POST {items, profile} -> {plans}
 *   scheduleUrl - POST {items, profile} -> {created, skipped, failed}
 *   when(plan)  - the page's own "day, start - stop" for a plan's padded window
 *   onDone(res) - after a create, with the server's answer
 */
function openBulkScheduleModal(opts) {
  const n = opts.items.length;
  const body = document.createElement('div');
  body.innerHTML = `
    <p>Each selected showing is scheduled as its own recording, with the profile below.</p>
    <div class="form-group">
      <label for="bs-profile">Recording Profile</label>
      <select id="bs-profile" class="form-control">
        <option value="default">Each showing's default profile</option>
        <option value="none">None (use global defaults)</option>
        ${opts.profiles.map((p) => `<option value="${p.id}">${escHtml(p.name)}</option>`).join('')}
      </select>
      <span class="form-hint">The profile's padding moves each start and stop, so the times
        and warnings below are re-checked when you change it.</span>
    </div>
    <div id="bs-summary"></div>
    <div id="bs-list"><p class="text-muted small">Loading…</p></div>`;

  let plans = [];
  let seq = 0;
  const modal = buildModal({
    title: `Schedule ${nf(n)} showing${n === 1 ? '' : 's'}`,
    body,
    panelClass: 'bs-panel',
    footer: [
      { label: 'Cancel', class: 'btn', onClick: (c) => c() },
      { label: 'Schedule', class: 'btn btn-primary', onClick: () => { submit(); return false; } },
    ],
  });
  const submitBtn = modal.querySelector('.modal-foot .btn-primary');
  const profileSel = body.querySelector('#bs-profile');
  const profileValue = () => {
    const v = profileSel.value;
    return v === 'default' || v === 'none' ? v : Number(v);
  };
  const payload = () => JSON.stringify({ items: opts.items, profile: profileValue() });
  const schedulable = () => plans.filter((p) => p.verdict !== 'skip');

  function conflictLines(w) {
    const names = (w.conflicts || []).slice(0, 3).map((c) => `${c.title} (${c.channel_name}`
      + `${c.in_batch ? ', also selected' : ''})`);
    const more = (w.conflicts || []).length > 3 ? ` and ${nf(w.conflicts.length - 3)} more` : '';
    return names.length ? ` ${names.join('; ')}${more}.` : '';
  }

  /* Hard first: a connection-limit warning names what will actually go wrong at record
     time (one of them waits for a free connection), where an overlap only says two things
     run at once. The account block has its own sentence from the server. */
  function warningHtml(plan) {
    const w = plan.warnings || {};
    const lines = [];
    if (w.connection_limit_warning) {
      lines.push(`<div class="bs-warn is-hard">${escHtml(w.connection_limit_warning.message)}${
        escHtml(conflictLines(w.connection_limit_warning))} When they overlap, one waits for a `
        + 'free connection and records less.</div>');
    }
    if (w.overlap_warning && !w.connection_limit_warning) {
      lines.push(`<div class="bs-warn">${escHtml(w.overlap_warning.message)}${
        escHtml(conflictLines(w.overlap_warning))}</div>`);
    }
    if (w.account_block_warning) {
      lines.push(`<div class="bs-warn">${escHtml(w.account_block_warning.message)}</div>`);
    }
    return lines.join('');
  }

  const VERDICT_BADGE = {
    ok: '',
    warn: '<span class="badge b-warn">Overlaps</span>',
    hard: '<span class="badge b-fail">Connection limit</span>',
    skip: '<span class="badge b-abort">Skipped</span>',
  };

  function rowHtml(plan) {
    const where = plan.group_name
      ? `${escHtml(plan.group_name)}${plan.member_name ? ` <span class="text-faint">via ${escHtml(plan.member_name)}</span>` : ''}`
      : escHtml(plan.channel_name || '');
    const skip = plan.verdict === 'skip';
    return `<li class="bs-row${skip ? ' is-skip' : ''}">
      <div class="bs-main">
        <span class="bs-title">${escHtml(plan.name || plan.title || `Showing #${plan.epg_id}`)}</span>
        ${VERDICT_BADGE[plan.verdict] || ''}
      </div>
      <div class="bs-meta">${where}${plan.start_time ? ` &middot; ${escHtml(opts.when(plan))}` : ''}${
        plan.started ? ' &middot; already on, records from now' : ''}${
        plan.profile_name ? ` &middot; ${escHtml(plan.profile_name)}` : ''}</div>
      ${skip ? `<div class="bs-warn is-skip">${escHtml(plan.reason || 'Not scheduled.')}</div>` : warningHtml(plan)}
    </li>`;
  }

  function render() {
    const go = schedulable().length;
    const count = (v) => plans.filter((p) => p.verdict === v).length;
    const parts = [];
    if (count('hard')) parts.push(`${nf(count('hard'))} over a connection limit`);
    if (count('warn')) parts.push(`${nf(count('warn'))} overlapping`);
    if (count('skip')) parts.push(`${nf(count('skip'))} skipped`);
    body.querySelector('#bs-summary').innerHTML = `<p class="bs-sum">${nf(go)} recording${
      go === 1 ? '' : 's'} will be scheduled${parts.length ? ` - ${parts.join(', ')}` : ''}.</p>`;
    body.querySelector('#bs-list').innerHTML = `<ul class="bs-list">${plans.map(rowHtml).join('')}</ul>`;
    submitBtn.disabled = go === 0;
    submitBtn.textContent = `Schedule ${nf(go)} recording${go === 1 ? '' : 's'}`;
  }

  async function preview() {
    const mine = ++seq;
    submitBtn.disabled = true;
    try {
      const data = await jsonFetch(opts.previewUrl, { method: 'POST', body: payload() });
      if (mine !== seq) return;
      plans = data.plans || [];
      render();
    } catch (err) {
      if (mine !== seq) return;
      body.querySelector('#bs-list').innerHTML = `<p class="notice notice-bad">${
        escHtml(err.message || 'Could not check these showings.')}</p>`;
    }
  }

  async function submit() {
    const idle = submitBtn.textContent;
    submitBtn.disabled = true;
    profileSel.disabled = true;
    submitBtn.textContent = 'Scheduling…';
    let res;
    try {
      res = await jsonFetch(opts.scheduleUrl, { method: 'POST', body: payload() });
    } catch (err) {
      showToast(err.message || 'Could not schedule these showings.', { type: 'error' });
      submitBtn.disabled = false;
      profileSel.disabled = false;
      submitBtn.textContent = idle;
      return;
    }
    const made = (res.created || []).length;
    const skipped = (res.skipped || []).length;
    const failed = res.failed || [];
    const msg = `${nf(made)} recording${made === 1 ? '' : 's'} scheduled`
      + (skipped ? `, ${nf(skipped)} skipped` : '')
      + (failed.length ? `, ${nf(failed.length)} failed` : '') + '.';
    if (opts.onDone) opts.onDone(res);
    if (!failed.length) {
      modal.closeModal();
      showToast(msg);
      return;
    }
    /* A failure stays on screen, by name and with the server's reason, rather than in a
       toast that is gone before it is read - the ones that worked are already scheduled. */
    body.innerHTML = `<p class="notice notice-bad">${escHtml(msg)} These were not scheduled:</p>
      <ul class="bs-list">${failed.map((p) => `<li class="bs-row">
        <div class="bs-main"><span class="bs-title">${escHtml(p.name || p.title || '')}</span>
          <span class="badge b-fail">Failed</span></div>
        <div class="bs-warn is-hard">${escHtml(p.error || 'Unknown error.')}</div></li>`).join('')}</ul>`;
    submitBtn.remove();
    showToast(msg, { type: 'error' });
  }

  profileSel.addEventListener('change', preview);
  preview();
  return modal;
}
