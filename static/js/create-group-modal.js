/* Shared "Create a channel group" modal (approved design: dev/changelog/322).
   Opened from the Groups list kebab (groups.js) and from a health check's detail page
   (group-detail.js). Replaces what used to be "Convert to channel group".

   Three things about it are load-bearing and must not drift:

   1. It CREATES. It posts the clone contract, so the source health check is left exactly
      as it is and the new group gets its own copy of the channel list. The old action
      flipped the group's type in place and deleted the pruned memberships.
   2. "does not match" is decided against the REFERENCE format - the highest-effective-score
      channel with a known format, id ascending as the tie-break - which is
      app/channel_groups.py::group_reference_key() for an unlocked group, and a source
      health check can never be locked. The previous modal compared against whichever
      tested channel happened to sort first in member order, so it flagged a different set
      than the group it created would actually use.
   3. The copy is NEVER put in the TV Guide, whatever the source's own status. Its members
      take the model defaults, so Recording is off on every one of them, and a guide row
      with nothing switched on for recording is what DESIGN-channel-groups-model.md §15
      refuses. The clone route refuses it independently and reports `guide_refused`; this
      screen agreeing with the rule is not what enforces it.

   Depends on util.js (escHtml, jsonFetch, showToast, buildModal, fieldRow).

   opts:
     srcId        - source group id (the health check's group)
     srcName      - source name, used in the title and the default group name
     channels     - [{id, name, account_name, account_color, score, resolution, fps, disabled}]
                    `score` is effective_score or null; `fps` must already be rounded to an
                    integer, because that is what format_key() compares on the server
     existingNames- lower-cased names already taken, for the client-side conflict check
     resolutionOptions / fpsOptions - the Manual format mode's dropdown contents
     defaultName  - overrides the default `${srcName} - channel group` Name value
                    (clone-modal.js passes `${srcName} (copy)`)
     initialSettings - {format_mode, format_resolution, format_fps, format_strategy}
                    from GET .../clone-info's `channel_settings`, to seed the settings
                    fieldset from the source's own current values instead of today's
                    hardcoded defaults (clone-modal.js, only present when the source
                    itself is already a recording group). `in_guide` is deliberately not
                    among them - see rule 3 below.
     onDone(resp) - called on success; default navigates to the new group's detail page
*/
function openCreateGroupModal(opts) {
  const channels = (opts.channels || []).slice();
  const taken = new Set((opts.existingNames || []).map(n => n.toLowerCase()));
  const kept = new Set(channels.map(c => c.id));
  const onDone = opts.onDone || ((resp) => { if (resp && resp.detail_url) location.href = resp.detail_url; });
  const seed = opts.initialSettings || null;

  // Auto-select-format plan (app/channel_groups.py::plan_format_selection via
  // format-plan.js). Fetched once on open; `state.autoSelect` is the chosen strategy
  // key ('' = None), `state.autoTouched` = true once the user hand-edits kept
  // membership or the format after picking a strategy (the honesty rule below).
  let planData = null, planLoading = true, planError = null;
  // rankScope 'all': this previews the format for a group that does not exist yet, whose
  // members all start Recording-off, so its own engine will rank over every member. The
  // default narrowing would rank over the SOURCE group's recording-enabled members, which
  // describes neither group (dev/changelog/890).
  fetchFormatPlan(opts.srcId, { rankScope: 'all' })
    .then((resp) => { planData = resp; planLoading = false; render(); })
    .catch((err) => { planError = err.message || 'failed to load'; planLoading = false; render(); });

  const UNSCORED_NEUTRAL = 50;   // app/channel_groups.py::effective_score
  const fmtKey = (c) => (c.resolution && c.fps ? `${c.resolution}@${c.fps}` : null);
  const fmtLabel = (k) => (k ? k.replace('@', ' @ ') : 'unknown');
  const score = (c) => (c.score == null ? UNSCORED_NEUTRAL : c.score);

  // group_reference_key(): highest effective score among candidates with a known format,
  // id ascending as the tie-break, manually-disabled members skipped. Recomputed on every
  // render, never frozen when the modal opens - removing the reference row moves it.
  function referenceOf(rows) {
    const known = rows.filter(c => fmtKey(c) && !c.disabled);
    if (!known.length) return null;
    return known.slice().sort((a, b) => (score(b) - score(a)) || (a.id - b.id))[0];
  }

  const body = document.createElement('div');
  let createBtn = null, footNote = null;
  const seedManual = seed && seed.format_mode === 'manual';
  const state = { name: opts.defaultName || `${opts.srcName} - channel group`,
                  mode: seed ? seed.format_mode : 'auto',
                  res: seedManual ? (seed.format_resolution || '') : '',
                  fps: seedManual ? String(seed.format_fps || '') : '',
                  formatStrategy: seed ? seed.format_strategy : 'health_check_only',
                  autoSelect: '', autoTouched: false };

  function nameError() {
    if (!state.name.trim()) return 'Give the group a name.';
    if (taken.has(state.name.trim().toLowerCase())) return `A group named "${state.name.trim()}" already exists.`;
    return null;
  }

  /* Exactly one state notice, and they are mutually exclusive. never_tested is its own
     state and must never fall through to "matches" - nothing can be checked for you when
     nothing has been measured, and saying "ready" there would be a lie. */
  function stateNotice(survivors, ref) {
    if (!survivors.length) {
      return { html: '<div class="notice notice-bad"><strong>A channel group needs at least one channel.</strong> ' +
        'Add one back below.</div>', block: 'Nothing is kept, so there is no group to create.' };
    }
    const known = survivors.filter(c => fmtKey(c));
    const untested = survivors.length - known.length;
    if (!known.length) {
      return { html: '<div class="notice notice-warn notice-act"><div><strong>None of these channels has a ' +
        'measured resolution or frame rate.</strong><p>Nothing can be checked for you: the group would be ' +
        'created on trust, and a feed that turns out not to match gets disabled the first time it is tested.</p>' +
        `<p>Running the check first takes a few minutes and tells you which of these ${survivors.length} ` +
        'channels actually belong together.</p></div>' +
        '<button class="btn btn-sm" data-cg="run-check">Run it now</button></div>', block: null };
    }
    const keys = [...new Set(known.map(fmtKey))];
    const refKey = fmtKey(ref);
    const bad = known.filter(c => fmtKey(c) !== refKey).length;
    if (keys.length > 1) {
      return { html: `<div class="notice notice-bad"><strong>${keys.length} different formats among these ` +
        `channels.</strong> ${bad} channel${bad === 1 ? ' does' : 's do'} not match. Remove ${bad === 1 ? 'it' : 'them'} ` +
        'below, or create the group anyway and they will be added but automatically taken out of failover.' +
        `<p>Compared against <strong>${escHtml(fmtLabel(refKey))}</strong>, the format of ` +
        `<strong>${escHtml(ref.name)}</strong> - the highest-scored channel that has been tested. That is the ` +
        'feed a recording would start on, so it is the one the rest of the group has to match.</p></div>',
        block: null, mismatch: bad };
    }
    return { html: `<div class="notice notice-ok"><strong>All ${known.length} tested channel` +
      `${known.length === 1 ? '' : 's'} share ${escHtml(fmtLabel(refKey))}.</strong> This group is ready to create.` +
      (untested ? ` <p>${untested} channel${untested === 1 ? ' has' : 's have'} never been tested, so ` +
        `${untested === 1 ? 'it is' : 'they are'} being taken on trust.</p>` : '') + '</div>', block: null };
  }

  let mismatchCount = 0;

  function autoSelectMeta() {
    if (planError) {
      return `Could not load format data: ${escHtml(planError)}. You can still build the group by hand below.`;
    }
    return 'Picks a video format from the health check\'s results, keeps the channels that report it, and ' +
      'locks the group to it. You can still adjust anything by hand afterward.';
  }

  function autoSelectControl() {
    if (planLoading) return '<select id="cg-auto" disabled><option>Loading&hellip;</option></select>';
    if (planError) return '<select id="cg-auto" disabled><option>Unavailable</option></select>';
    const opts_ = ['<option value=""' + (state.autoSelect === '' ? ' selected' : '') +
      '>None - keep every channel</option>'].concat(
      FORMAT_STRATEGY_KEYS.map((key) =>
        `<option value="${key}"${state.autoSelect === key ? ' selected' : ''}>` +
        `${escHtml(formatPlanOptionLabel(planData, key))}</option>`));
    return `<select id="cg-auto">${opts_.join('')}</select>`;
  }

  // The bucket table (every measured format, winner marked) plus a one-line summary,
  // shown once a strategy is picked. Never omitted for a no-winner strategy - the
  // rationale line says why, per the failure-paths-must-be-observable rule.
  function autoSelectDetails() {
    if (!planData || !state.autoSelect) return '';
    const entry = planData.strategies[state.autoSelect];
    const table = formatPlanTable(planData, state.autoSelect);
    let summary;
    if (!entry || !entry.key) {
      summary = entry ? entry.rationale : 'No format has enough healthy channels to build a group on.';
    } else {
      const nonMatch = planData.eligible_count - entry.count;
      const excluded = planData.excluded_count;
      const parts = [`Keeps ${entry.count} of ${planData.total} channels.`];
      if (nonMatch > 0) parts.push(`${nonMatch} do${nonMatch === 1 ? 'es' : ''} not match.`);
      if (excluded > 0) {
        parts.push(`${excluded} ${excluded === 1 ? 'is' : 'are'} excluded because ${excluded === 1 ? 'it' : 'they'} ` +
          `failed, ${excluded === 1 ? 'was' : 'were'} cancelled, or ${excluded === 1 ? 'has' : 'have'} never been tested.`);
      }
      summary = parts.join(' ');
    }
    if (state.autoTouched) {
      summary += ` You have edited this by hand, so it no longer matches ` +
        `"${groupStrategyLabel(state.autoSelect)}" exactly.`;
    }
    return `<div class="cg-auto-details">${table}<p class="text-muted">${escHtml(summary)}</p></div>`;
  }

  function render() {
    const survivors = channels.filter(c => kept.has(c.id));
    const ref = referenceOf(survivors);
    const refKey = fmtKey(ref);
    const note = stateNotice(survivors, ref);
    mismatchCount = note.mismatch || 0;
    const measured = survivors.filter(c => fmtKey(c)).length;

    // Channel ids the active auto-select strategy would keep - marks rows below with an
    // "auto-selected" chip regardless of whether the user has since hand-edited kept
    // membership (that's provenance, not a claim about the current state).
    const autoEntry = planData && state.autoSelect ? planData.strategies[state.autoSelect] : null;
    const autoIds = new Set(autoEntry && autoEntry.key ? autoEntry.channel_ids : []);

    /* A fixed five-track grid, not a flex line: every row has a format cell and a flag
       cell whether or not they are filled, so the flag cannot shift the format value. */
    const rows = channels.map(c => {
      const isKept = kept.has(c.id);
      const k = fmtKey(c);
      const isRef = ref && c.id === ref.id;
      const bad = isKept && refKey && k && k !== refKey;
      const isPicked = isKept && autoIds.has(c.id);
      let flag = '';
      if (isKept && isRef) {
        flag = '<span class="pr-flag pr-refmark tip-plain" data-tip="Reference format.&#10;The highest-scored ' +
          'channel that has been tested. Every other channel is compared against this one.">reference</span>';
      } else if (bad) {
        flag = '<span class="pr-flag pr-nomatch tip-plain" data-tip="Does not match.&#10;This feed reports a ' +
          'different resolution or frame rate than the reference, so failover onto it produces a file that ' +
          'will not play cleanly.">does not match</span>';
      } else if (isKept && !k) {
        flag = '<span class="pr-flag pr-untested tip-plain" data-tip="Never tested.&#10;No health check has ' +
          'measured this feed, so nothing can be compared. It is being taken on trust.">never tested</span>';
      }
      if (isPicked) {
        flag += ' <span class="pr-flag pr-picked tip-plain" data-tip="Auto-selected.&#10;Kept by the ' +
          `${escHtml(groupStrategyLabel(state.autoSelect))} strategy you picked above.">auto-selected</span>`;
      }
      return `<div class="prune-row${bad ? ' bad' : ''}${isKept && ref && c.id === ref.id ? ' ref' : ''}` +
        `${isPicked ? ' picked' : ''}${isKept ? '' : ' out'}">` +
        `<span class="acct-dot tip-plain" data-tip="${escHtml(c.account_name || 'Account')}" ` +
          `style="background:${escHtml(c.account_color || 'var(--text-faint)')}"></span>` +
        `<span class="pr-name"><span class="pr-nm">${escHtml(c.name)}</span>` +
          `<span class="pr-acct">${escHtml(c.account_name || '')}${c.score == null ? '' : ` &middot; score ${c.score}`}</span></span>` +
        `<span class="pr-fmt">${k ? escHtml(fmtLabel(k)) : '--'}</span>` +
        `<span class="pr-flagcell">${flag}</span>` +
        `<button class="btn btn-sm" data-prune="${c.id}">${isKept ? 'Remove' : 'Add back'}</button></div>`;
    }).join('');

    const resOpts = (opts.resolutionOptions || []).map(r =>
      `<option value="${escHtml(r)}"${r === state.res ? ' selected' : ''}>${escHtml(r)}</option>`).join('');
    const fpsOpts = (opts.fpsOptions || []).map(f =>
      `<option value="${f.value}"${String(f.value) === String(state.fps) ? ' selected' : ''}>${escHtml(f.label)}</option>`).join('');
    // Does the SOURCE manage a format? A health-check-only source has none to copy, and
    // no member has been measured, so the format half of this screen has nothing to
    // answer with (DESIGN-channel-groups-model.md §14).
    const managesFormat = state.formatStrategy !== 'health_check_only';

    body.innerHTML =
      '<div class="notice notice-info">' +
        '<strong>A channel group bundles identical feeds of one channel into a single TV Guide row.</strong> ' +
        'Recordings from it start on the highest-scored feed and fail over to the next best one if that feed ' +
        'dies, so a dropped stream becomes a seam in the file instead of a truncated recording.' +
        '<p>Channels of different formats can all live in one group. The group format decides which of ' +
        'them a recording may be picked from - the rest stay in the group and are skipped until they ' +
        'match again.</p>' +
        `<p>The health check "${escHtml(opts.srcName)}" is not touched - this creates a new group alongside ` +
        'it, and takes you to that group when it is done.</p>' +
      '</div>' + note.html +
      '<fieldset class="gd-fset"><div class="gd-fset-head">Channel group</div>' +
      fieldRow({ label: 'Name', wide: true,
        meta: 'How the group shows up in the group list and, if it is in the guide, as the guide row title. ' +
          'Group names are unique across channel groups and health checks alike.',
        control: `<input type="text" id="cg-name" value="${escHtml(state.name)}">` }) +
      // The TV Guide is not offered here. A copy's members take the model defaults -
      // Recording off on every one (§14) - so a copy that joined the guide would be a row
      // with nothing behind it, which §15 refuses; the server refuses it too. What
      // replaces the switch is the sentence saying where the guide question is answered.
      fieldRow({ label: 'The TV Guide',
        meta: 'The copy is created out of the guide, with Recording off on every member - ' +
          'the same way any new group starts. Add it to the guide from its own page once ' +
          'you have chosen which members it may record from.' }) +
      // The five format questions only mean something for a source that already manages a
      // format. Cloning a health check has no measured members to reason about, and every
      // strategy would answer "run a check first" (§14).
      (managesFormat
        ? fieldRow({ label: 'Auto-select channels',
            meta: autoSelectMeta(),
            control: autoSelectControl() }) +
          autoSelectDetails() +
          fieldRow({ label: 'Group format',
            meta: '<p><strong>Automatic</strong> takes the format from the highest-scored channel and updates it ' +
              'if that channel changes. <strong>Manual</strong> pins it to exactly what you pick below and ignores ' +
              'provider-side changes.</p>',
            control: `<select id="cg-mode"><option value="auto"${state.mode === 'auto' ? ' selected' : ''}>Automatic</option>` +
              `<option value="manual"${state.mode === 'manual' ? ' selected' : ''}>Manual</option></select>` }) +
          fieldRow({ id: 'res', sub: true, hide: state.mode !== 'manual', label: 'Resolution',
            meta: 'Channels reporting anything else count as mismatched.',
            control: `<select id="cg-res">${resOpts}</select>` }) +
          fieldRow({ id: 'fps', sub: true, hide: state.mode !== 'manual', label: 'Frame rate',
            meta: '29.97 and 30 (and 59.94 and 60) are treated as the same rate.',
            control: `<select id="cg-fps">${fpsOpts}</select>` })
        : fieldRow({ label: 'Format', full: true,
            meta: 'The source is set up for health checks only, so there is no format to ' +
              'copy and nothing has been measured yet to choose one from. The copy is ' +
              'created the same way, and its format strategy is chosen on its own page ' +
              'once a health check has run.' })) +
      '</fieldset>' +
      '<fieldset class="gd-fset"><div class="gd-fset-head">Channels' +
        `<span class="fh-note">${survivors.length} of ${channels.length} kept, ${measured} measured</span></div>` +
      fieldRow({ full: true, label: 'What removing does',
        meta: 'Removing a channel here leaves it in the health check. It only means the new group will not ' +
          'contain it.' }) +
      `<div class="prune-list">${rows}</div></fieldset>`;

    const nameErr = nameError();
    if (createBtn) createBtn.disabled = !!(note.block || nameErr);
    if (footNote) {
      footNote.textContent = note.block || nameErr ||
        (mismatchCount ? 'You will be asked to confirm the mismatches.' : '');
    }
  }

  body.addEventListener('click', (e) => {
    const prune = e.target.closest('[data-prune]');
    if (prune) {
      const id = parseInt(prune.dataset.prune, 10);
      if (kept.has(id)) kept.delete(id); else kept.add(id);
      if (state.autoSelect) state.autoTouched = true;
      render();
      return;
    }
    if (e.target.closest('[data-cg="run-check"]')) {
      if (!opts.jobId) { showToast('This health check has no run to start.', { type: 'error' }); return; }
      jsonFetch(`/api/channel-tests/on-demand/${opts.jobId}/start`, { method: 'POST' })
        .then(() => { showToast('Health check started. Come back once it has run.'); location.reload(); })
        .catch(err => showToast(err.message, { type: 'error' }));
    }
  });

  // Delegated, because render() replaces the controls on every change.
  body.addEventListener('input', (e) => {
    if (e.target.id === 'cg-name') {
      state.name = e.target.value;
      const err = nameError();
      if (createBtn) createBtn.disabled = !!err || !kept.size;
      if (footNote) footNote.textContent = err || (!kept.size ? 'Nothing is kept, so there is no group to create.' : '');
    }
  });
  body.addEventListener('change', (e) => {
    if (e.target.id === 'cg-res') {
      state.res = e.target.value;
      if (state.autoSelect) { state.autoTouched = true; render(); }
      return;
    }
    if (e.target.id === 'cg-fps') {
      state.fps = e.target.value;
      if (state.autoSelect) { state.autoTouched = true; render(); }
      return;
    }
    if (e.target.id === 'cg-mode') {
      state.mode = e.target.value;
      if (state.mode === 'manual') {
        if (!state.res) state.res = (opts.resolutionOptions || [])[0] || '';
        if (!state.fps) state.fps = String(((opts.fpsOptions || [])[0] || {}).value || '');
      }
      if (state.autoSelect) state.autoTouched = true;
      render();
      return;
    }
    if (e.target.id === 'cg-auto') {
      const value = e.target.value;
      state.autoSelect = value;
      state.autoTouched = false;
      const entry = planData && planData.strategies[value];
      if (value && entry && entry.key) {
        kept.clear();
        entry.channel_ids.forEach(id => kept.add(id));
        state.mode = 'manual';
        state.res = entry.resolution;
        state.fps = String(entry.fps);
      } else if (!value) {
        channels.forEach(c => kept.add(c.id));
      }
      render();
    }
  });

  function post(allowMismatch) {
    return jsonFetch(`/api/channel-groups/${opts.srcId}/clone`, {
      method: 'POST',
      body: JSON.stringify({
        name: state.name.trim(),
        channel_ids: Array.from(kept),
        // Never in the guide: §15 forbids a guide row with no recording-enabled member,
        // and a copy has none. The server refuses it too - this is the client agreeing
        // with the rule rather than the only thing enforcing it.
        in_guide: false,
        // Pinning a format IS the `manual` strategy - it is the only one whose lock
        // belongs to the user, and the server refuses to store a pin under any other
        // (dev/changelog/762). Sending the source's strategy alongside a pin used to
        // produce a clone whose lock the next health check would overwrite, which made
        // this screen's own "Manual pins it to exactly what you pick below and ignores
        // provider-side changes" untrue.
        format_strategy: state.mode === 'manual' ? 'manual' : state.formatStrategy,
        format_resolution: state.mode === 'manual' ? state.res : null,
        format_fps: state.mode === 'manual' ? state.fps : null,
        allow_format_mismatch: !!allowMismatch,
      }),
    });
  }

  function confirmMismatch(onYes) {
    const n = mismatchCount;
    const outcome = 'They stay in the group, and the format lock filters them out whenever ChannelBin ' +
      'picks a member - so nothing records from them while they do not match, and they become eligible ' +
      'again on their own if they start matching. Nothing is switched off.';
    buildModal({
      title: 'Create with mismatched channels?',
      panelClass: 'modal-wide',
      body: `<p><strong>${n} channel${n === 1 ? '' : 's'} in this group ${n === 1 ? 'does' : 'do'} not match ` +
        'the group format.</strong></p>' + `<p class="text-muted">${outcome}</p>` +
        '<p class="text-muted">Removing them instead keeps the group clean, and they stay in the health ' +
        'check either way.</p>',
      footer: [
        { label: 'Go back', class: 'btn', onClick: (c) => c() },
        { label: 'Create anyway', class: 'btn btn-danger', onClick: (c) => { c(); onYes(); return false; } },
      ],
    });
  }

  const modal = buildModal({
    title: `Create a channel group from "${opts.srcName}"`,
    panelClass: 'modal-xwide',
    body,
    footNote: ' ',
    footer: [
      { label: 'Cancel', class: 'btn', onClick: (c) => c() },
      { label: 'Create channel group', class: 'btn btn-primary', onClick: (close) => {
        const submit = (allow) => post(allow)
          .then((resp) => {
            // The server owns the mismatch warning; this modal's own count only pre-empts
            // it (dev/changelog/762). If the server saw a mismatch this screen did not -
            // a test that landed while the modal was open - it says so instead of creating.
            if (!resp.success) { confirmMismatch(() => submit(true)); return; }
            showToast(`Created "${state.name.trim()}".`); close(); onDone(resp);
          })
          .catch(err => showToast(err.message, { type: 'error' }));
        if (mismatchCount) { confirmMismatch(() => submit(true)); return false; }
        submit(false);
        return false;
      } },
    ],
  });
  footNote = modal.footNote;
  createBtn = modal.querySelector('.modal-foot .btn-primary');
  render();
  return modal;
}
