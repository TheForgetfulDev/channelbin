/* The promotion walkthrough (DESIGN-channel-groups-model.md §14.1, approved arrangement:
   dev/mockups/33-group-detail-desktop.html round 3.1).

   A group is created as a health check and promoted deliberately (§14). This is the
   dialog that promotes it, opened the moment the user reaches for a recording action on a
   group whose strategy is still `health_check_only`: the guide button, the first Recording
   switch, or the bulk Recording-on action. It never refuses - it asks the questions the
   action needs answered, then completes the action that opened it.

   Three things about it are load bearing and must not drift:

   1. **It submits ONE request.** The four decisions (strategy + pin, which members record,
      whether unmatched members keep being checked, whether it joins the guide) go to
      POST .../promote together. A client-side Promise.all over four endpoints is how
      dev/changelog/756's Save silently discarded three writes when the first failed, and a
      half-promoted group is a genuinely bad state: a guide row whose members were never
      switched on, or members switched on for a lock that never got written.
   2. **The bucket table and the option labels come from the server**, through
      format-plan.js and GET .../format-plan. The mockup reimplemented the whole bucket
      engine in JS because a mockup has no server; here that would be a second answer to
      "which format wins" for the user to notice disagreeing with the first.
   3. **The two bulk offers compose into the bad setup on purpose, and the warning names
      the COMBINATION** rather than each half (§14.1). "Record from all" plus "stop
      checking the ones that do not match" produces recording-enabled, unmonitored,
      mismatched members - allowed, as the model consistently allows them, but never quiet.

   Depends on util.js (escHtml, jsonFetch, showToast, buildModal, fieldRow) and
   format-plan.js (fetchFormatPlan and the GROUP_FORMAT_STRATEGIES table).

   opts:
     groupId, groupName - the group being promoted
     rows        - the member rows as the page has them; each needs {channel_id,
                   channel_name, last_test:{resolution, fps}} so the two bulk offers can
                   count what matches without a second round trip
     derived     - the group's effective format reference label, for highest_score
     inGuide     - whether it already holds a guide row (step 3 is skipped if so)
     trigger     - 'guide' | 'member' | 'bulk', what the user clicked to get here
     pendingIds  - channel ids that click was going to switch on, completed on submit
     onDone(resp)- called after a successful promote
*/
function openPromoteModal(opts) {
  const rows = opts.rows || [];
  const total = rows.length;
  const pending = (opts.pendingIds || []).slice();
  const onDone = opts.onDone || (() => location.reload());

  // Deliberately not `health_check_only`: this dialog exists because the user reached for
  // a recording action, and offering "not a recording source" as the answer would be
  // offering to do nothing.
  const state = {
    strategy: 'highest_score', pin: null, enable: 'matching',
    unmatched: 'keep', addGuide: !opts.inGuide,
    plan: null, planState: 'loading', busy: false,
  };

  const fmtKey = (r) => {
    const t = r.last_test;
    if (!t || !t.resolution) return null;
    return t.fps ? `${t.resolution} @ ${Math.round(t.fps)}` : t.resolution;
  };

  // The winning format for the current choice, as a bucket label - the same string
  // formatPlanEntry works in, so the counts below and the table's winner marker agree.
  const winner = () => {
    const e = formatPlanEntry(state.plan, state.strategy, state.pin, opts.derived || null);
    return (e && e.key) ? e.label : null;
  };

  // An unmeasured member is never counted as non-matching. Unknown is not
  // proven-different (§5's untested-member rule), and offering to stop health checking it
  // for failing a comparison nothing could make is how a member ends up recordable with
  // no data behind it forever. The server applies the same rule.
  const matching = () => { const k = winner(); return k ? rows.filter(r => fmtKey(r) === k) : []; };
  const unmatched = () => {
    const k = winner();
    return k ? rows.filter(r => fmtKey(r) && fmtKey(r) !== k) : [];
  };

  const body = document.createElement('div');
  let submitBtn = null;

  fetchFormatPlan(opts.groupId)
    .then((resp) => { state.plan = resp; state.planState = 'ready'; draw(); })
    .catch(() => { state.planState = 'error'; draw(); });

  function radio(name, value, current, title, meta) {
    return `<label class="fp-radio"><input type="radio" name="${name}" value="${value}"` +
      `${value === current ? ' checked' : ''}> <strong>${escHtml(title)}</strong>` +
      `<span class="text-muted">${escHtml(meta)}</span></label>`;
  }

  function draw() {
    const loading = state.planState === 'loading';
    const key = winner();
    const nMatch = matching().length;
    const nUnmatched = unmatched().length;
    const fmtName = key || 'the chosen format';

    let h = `<p class="card-note">${escHtml(opts.groupName)} is set up for health checks ` +
      'only. To record from it, ChannelBin needs to know which video format the group ' +
      `should be, and which of its ${total} member${total === 1 ? '' : 's'} it may record from.</p>`;

    h += '<fieldset class="gd-fset"><div class="gd-fset-head">' +
      '1. Which format should this group be?</div>';
    h += fieldRow({
      wide: true,
      label: '',
      meta: `<div class="gd-strategy-help${groupStrategyManagesFormat(state.strategy) ? '' : ' none'}">` +
        `${escHtml(groupStrategyHelp(state.strategy))}</div>`,
      control: `<select id="pm-strategy"${loading ? ' disabled' : ''}>` +
        (loading ? '<option>Loading&hellip;</option>'
          : GROUP_FORMAT_STRATEGIES
            .filter(([k]) => k !== 'health_check_only')
            .map(([k]) => `<option value="${k}"${k === state.strategy ? ' selected' : ''}>` +
              `${escHtml(formatPlanOptionLabel(state.plan, k, opts.derived || null))}</option>`)
            .join('')) + '</select>',
    });

    // `manual` is the one value that carries a second question. Round 3 of the mockup
    // shipped it without one, which is the same omission round 1 had in Settings.
    if (state.strategy === 'manual') {
      h += fieldRow({
        wide: true,
        label: 'Pinned format',
        meta: 'The format this group is locked to. Nothing moves it - not a health check ' +
          'run, not a provider change.',
        control: formatPlanPinSelect('pm-pin', state.plan, state.pin),
      });
    }

    h += fieldRow({
      full: true,
      label: '',
      meta: escHtml(formatPlanSummary(state.plan, state.strategy, state.pin,
                                      opts.derived || null, total,
                                      rows.filter(r => fmtKey(r)).length)) +
        (state.planState === 'ready' && groupStrategyManagesFormat(state.strategy)
          ? `<div class="cg-auto-details">${formatPlanTable(state.plan, state.strategy, state.pin, opts.derived || null)}</div>`
          : '') +
        (state.planState === 'error'
          ? '<p class="text-muted">The measured formats could not be loaded, so the table ' +
            'is unavailable. The strategy itself still saves.</p>'
          : ''),
    });
    h += '</fieldset>';

    h += '<fieldset class="gd-fset"><div class="gd-fset-head">' +
      '2. Which members may it record from?</div>';
    h += fieldRow({
      full: true,
      label: '',
      meta:
        radio('pm-enable', 'matching', state.enable,
              `The ${nMatch} member${nMatch === 1 ? '' : 's'} that match ${fmtName}`,
              'The conservative choice. You can switch the others on by hand at any time.') +
        radio('pm-enable', 'all', state.enable, `All ${total} member${total === 1 ? '' : 's'}`,
              `Recording will be turned on for all ${total} member${total === 1 ? '' : 's'}. ` +
              `The ${nUnmatched} that do not match ${fmtName} will not be used for a ` +
              'recording unless a later change makes them eligible.') +
        radio('pm-enable', 'none', state.enable, 'None - I will pick them myself',
              'The group keeps the strategy and stays out of the guide until you switch ' +
              'at least one member on.'),
    });

    if (nUnmatched > 0) {
      h += fieldRow({
        full: true,
        label: `The ${nUnmatched} member${nUnmatched === 1 ? '' : 's'} that do not match`,
        meta:
          radio('pm-unmatched', 'keep', state.unmatched, 'Keep checking them',
                'They stay monitored, so ChannelBin notices if their format changes and ' +
                'they become usable.') +
          radio('pm-unmatched', 'stop', state.unmatched, 'Turn their health check off',
                'They will not be checked again. Consider turning recording off on them, ' +
                'or removing them from the group - otherwise, if their format matches ' +
                'later, they become eligible for a recording with no health data behind them.'),
      });
    }
    h += '</fieldset>';

    if (!opts.inGuide) {
      h += '<fieldset class="gd-fset"><div class="gd-fset-head">' +
        '3. Add it to the TV Guide?</div>';
      h += fieldRow({
        label: '',
        meta: 'A group in the guide fills one row and can be recorded on a schedule. You ' +
          'can add it later instead.',
        control: '<label class="switch"><input type="checkbox" id="pm-guide"' +
          `${state.addGuide ? ' checked' : ''}><span class="knob"></span></label>`,
      });
      h += '</fieldset>';
    }

    // §14.1: named as ONE outcome rather than as two separate warnings, because it is one
    // outcome - the user cannot see the combination by reading either half.
    if (state.enable === 'all' && state.unmatched === 'stop' && nUnmatched > 0) {
      h += `<div class="notice notice-warn"><strong>&#9888; ${nUnmatched} member` +
        `${nUnmatched === 1 ? '' : 's'} will be set to record without being monitored.</strong><br>` +
        'That is allowed, and the member list will keep saying so. ChannelBin may pick ' +
        `${nUnmatched === 1 ? 'it' : 'one of them'} for a recording without knowing whether ` +
        `${nUnmatched === 1 ? 'it' : 'they'} still work.</div>`;
    }

    body.innerHTML = h;
    if (submitBtn) submitBtn.disabled = state.busy || loading;
  }

  draw();

  body.addEventListener('change', (e) => {
    const t = e.target;
    if (t.id === 'pm-strategy') {
      state.strategy = t.value;
      // Seed the pin from whatever the group would otherwise be, so switching to `manual`
      // never lands on an empty control the user has to discover is empty.
      if (state.strategy === 'manual' && !state.pin) {
        const buckets = (state.plan && state.plan.buckets) || [];
        state.pin = opts.derived || (buckets[0] && buckets[0].label) || null;
      }
      draw();
      return;
    }
    if (t.id === 'pm-pin') { state.pin = t.value; draw(); return; }
    if (t.name === 'pm-enable') { state.enable = t.value; draw(); return; }
    if (t.name === 'pm-unmatched') { state.unmatched = t.value; draw(); return; }
    if (t.id === 'pm-guide') { state.addGuide = t.checked; }
  });

  const modal = buildModal({
    title: `Set up "${opts.groupName}" for recording`,
    body,
    panelClass: 'modal-xwide',
    footer: [
      { label: 'Cancel', class: 'btn', onClick: (close) => close() },
      { label: 'Set it up', class: 'btn btn-primary', onClick: (close) => {
        if (state.busy) return false;
        state.busy = true;
        submitBtn.disabled = true;
        const pin = state.strategy === 'manual' ? (state.pin || '') : null;
        const parts = pin ? pin.split(' @ ') : [];
        jsonFetch(`/api/channel-groups/${opts.groupId}/promote`, {
          method: 'POST',
          body: JSON.stringify({
            strategy: state.strategy,
            resolution: parts[0] || null,
            fps: parts[1] ? Number(parts[1]) : null,
            enable: state.enable,
            enable_channel_ids: pending,
            unmatched_checks: state.unmatched,
            // The trigger completes itself: clicking the guide button IS the request to
            // be in the guide, whatever step 3's switch says - and step 3 is not even
            // rendered for a group already in it.
            add_to_guide: opts.trigger === 'guide' || state.addGuide,
          }),
        }).then((resp) => {
          close();
          promoteToast(opts.groupName, resp);
          onDone(resp);
        }).catch((err) => {
          state.busy = false;
          submitBtn.disabled = false;
          showToast(err.message || 'Could not set this group up.', { type: 'error' });
        });
        return false;
      } },
    ],
  });
  submitBtn = modal.querySelector('.modal-foot .btn-primary');
  if (submitBtn) submitBtn.disabled = state.planState === 'loading';
  return modal;
}

/* What the user is owed after a promotion. Three outcomes, and the one that needs saying
   most is the middle one: a promotion that switched nothing on leaves the group out of
   the guide, and without a sentence the only feedback is a dialog closing. */
function promoteToast(name, resp) {
  if (resp.joined_guide) {
    showToast(`${name} is set up for recording and now fills one TV Guide row.`,
              { type: 'success' });
    return;
  }
  if (!resp.recording_members) {
    showToast(`Format strategy set. No member is switched on for recording yet, so ${name} ` +
      'stays out of the TV Guide until one is.', { type: 'warning' });
    return;
  }
  showToast(`${name} is set up for recording. Add it to the TV Guide whenever you want a ` +
    'row for it.', { type: 'success' });
}
