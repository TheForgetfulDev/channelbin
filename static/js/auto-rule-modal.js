/* Auto-record rules: the rule dialog (create and edit) and the actions every Rules surface
   shares - pause, resume, check now and delete (DESIGN-autoschedule.md §8, mockup 42,
   dev/changelog/1189). Loaded by the Programs search (static/js/channel-search.js), the
   Rules list (static/js/rules.js) and a rule's page (static/js/rule-detail.js), so a rule
   is worded and acted on one way everywhere. Depends on util.js (escHtml, nf, jsonFetch,
   buildModal, fieldRow, showToast).

   Five things to keep in mind:

   * The criteria ARE the search. On create, `search` is the page's own snapshot() string,
     the spelling a saved search stores; the server drops the page-only parts, refuses what a
     rule cannot use (§3.1) and stores the canonical form. Edit never sends a search - a
     rule's search is changed on the Programs search itself (its Save to rule), so the
     dialog shows it read-only with the way there. A refusal is shown inside the dialog and
     the dialog stays open, because the sentence says what to change.
   * A changed search that no longer matches some of the rule's scheduled recordings is not
     saved until the person says what becomes of them (openAutoRuleUnmatched, §8,
     dev/changelog/1203). The server asks - its 409 names them - and decides; this file
     only shows the question and sends the answer back.
   * The client-side "set both or neither" check is presentation - it mirrors
     app/auto_rules.py::read_rule_values, which is the enforcement.
   * A save, a resume and a check run the rule's check in the same request (§4.1), so the
     toast says what that check did - or, when other work holds it back, what it is waiting
     for. A rule that silently scheduled nothing is the failure this feature exists to avoid.
   * Delete names only the rule. Calling off its SCHEDULED recordings is Cancel (DESIGN.md §4,
     dev/changelog/814), offered as a choice that defaults to keeping them; anything that
     already started or finished is never touched, whichever is chosen, and the dialog says
     so. It is a buildModal of its own because it carries that choice (DESIGN.md 3.12).

   The helpers are at file top level so tests/test_auto_rule_modal_js.py can call them. */

/* "2 scheduled, 1 repeat skipped" for a {action: count} dict, over the page's phrase table
   (routes/auto_rules.py::VERDICT_PHRASES, served through each page's config). */
function autoRuleCountsText(counts, phrases) {
  return (phrases || [])
    .map((p) => {
      const n = Number((counts || {})[p.action]) || 0;
      if (!n) return '';
      return (n === 1 ? p.one : p.many).replace('{n}', nf(n));
    })
    .filter(Boolean)
    .join(', ');
}

/* What one evaluation did, as {text, type} for a toast. `lead` names what just happened
   ("Rule saved."); `evaluation` is auto_rules.evaluate_now()'s answer - {queued,
   waiting_for} when admission held it back, else the rule's RuleOutcome payload. */
function autoRuleOutcome(lead, evaluation, phrases) {
  const ev = evaluation || {};
  if (ev.queued) {
    return {
      type: 'info',
      text: `${lead} It has not been checked yet: ${ev.waiting_for || 'other work is running'}. `
        + 'The check runs as soon as that finishes.',
    };
  }
  // `error` is a check that did not run at all; `problems` names showings a check that DID
  // run could not place - reported after what it scheduled, never instead of it.
  if (ev.error) return { type: 'error', text: `${lead} Its check stopped: ${ev.error}` };
  if (ev.abandoned) {
    return { type: 'warning',
             text: `${lead} Its check was cut short by another change to the rule. The next check picks it up.` };
  }
  // The payload carries the picks as a count of their own and the skips by verdict.
  const pick = (phrases || []).find((p) => p.pick);
  const counts = Object.assign({}, ev.skipped || {});
  if (pick) counts[pick.action] = ev.scheduled || 0;
  const parts = [autoRuleCountsText(counts, phrases)];
  const already = Number(ev.already_scheduled) || 0;
  if (already) parts.push(`${nf(already)} already scheduled`);
  // Recordings the check moved because the guide moved their program (§5) - said, never
  // done silently.
  const moved = Number(ev.retracked) || 0;
  if (moved) parts.push(`${nf(moved)} moved to a new time`);
  const said = parts.filter(Boolean).join(', ');
  if (ev.problems) {
    return { type: 'warning', text: `${lead} ${said ? `${said}. ` : ''}${ev.problems}` };
  }
  if (said) return { type: 'success', text: `${lead} ${said}.` };
  // A null reach after a check means what the rule searches held no listings at all - the
  // empty-guide case (§4.5), which must not read as "nothing matched".
  if ('guide_reach' in ev && !ev.guide_reach) {
    return { type: 'warning',
             text: `${lead} Nothing to schedule yet - the guide holds no upcoming listings for it to search.` };
  }
  return { type: 'success', text: `${lead} Nothing upcoming matches it yet.` };
}

/* A number box's value for the API: null when empty, a number when it is a whole number,
   and otherwise the text itself, so the server's own sentence says what is wrong with it
   rather than this file quietly rounding it. */
function autoRuleNumber(raw) {
  const s = String(raw == null ? '' : raw).trim();
  if (s === '') return null;
  return /^\d+$/.test(s) ? Number(s) : s;
}

const _arCount = (n, one, many) => `${nf(n)} ${n === 1 ? one : many}`;

/* The edit dialog's field to land on when a Settings chip opened it. */
const AUTO_RULE_FOCUS = {
  name: '#ar-name', profile: '#ar-profile', repeats: '#ar-repeats',
  limit: '#ar-limit-count', cap: '#ar-cap',
};

/**
 * The rule dialog. Create, from a Programs search:
 *   search      - the search's canonical query string (channel-search.js snapshot())
 *   summary     - optional one-line description of that search
 *   name        - the default rule name
 *   createUrl   - POST target (/api/auto-rules)
 *   rulesUrl    - the Rules page, linked from the toast when the answer names no rule page
 *   goToRule    - open the new rule's page once it is made, carrying the toast there
 * Edit, from a Rules surface:
 *   rule        - auto_rules_view.dialog_data(): {id, name, profile_id, skip_repeats,
 *                 limit_count, limit_window_hours, max_upcoming, search_lines,
 *                 edit_search_url}
 *   focus       - which field to land on (AUTO_RULE_FOCUS), from the chip that opened it
 *   apiBase     - /api/auto-rules/ (PUT apiBase + id)
 * Both:
 *   profiles    - [{id, name}] every recording profile
 *   defaultCap  - auto_scheduling.default_max_upcoming (0 = no cap)
 *   phrases     - routes/auto_rules.py::verdict_phrases()
 *   onSaved(res) - after a successful save, with the server's answer
 */
function openAutoRuleModal(opts) {
  const rule = opts.rule || null;
  const edit = !!rule;
  const cap = Number(opts.defaultCap) || 0;
  const capDefault = cap ? `the default of ${nf(cap)}` : 'the default, which is no cap';
  const numberValue = (v) => (v === null || v === undefined ? '' : escHtml(String(v)));
  const body = document.createElement('div');
  body.innerHTML =
    '<p class="modal-err" id="ar-err" hidden></p>' +
    (edit ? '' :
      '<p class="card-note">Each upcoming program this search finds is recorded once - now, and ' +
      'again whenever the guide refreshes. A program on several channels at once is recorded ' +
      'from one of them (a channel group first, then the healthiest), and repeats and anything ' +
      'past the cap are left out and listed on the rule\'s page with the reason. Open the ' +
      'rule\'s search from there at any time to see exactly what it matches.' +
      (opts.summary ? ` Search: ${escHtml(opts.summary)}.` : '') + '</p>') +
    fieldRow({
      label: 'Name', stack: true,
      meta: 'How the rule is listed on the Rules page.',
      control: `<input type="text" id="ar-name" maxlength="255" value="${escHtml(edit ? rule.name : (opts.name || ''))}">`,
    }) +
    (edit ? fieldRow({
      label: 'Search', stack: true,
      meta: 'What the rule records. The search is the rule.',
      control: '<div class="ar-searchbox"><div class="statlist rl-search-lines">' +
        (rule.search_lines || []).map(([k, v]) => `<div class="srow"><span class="sk">${escHtml(k)}</span>` +
          `<span class="sv">${escHtml(v)}</span></div>`).join('') +
        '</div><div class="ar-searchbox-foot"><span>Editing it opens the Programs search with this ' +
        'search loaded.</span>' +
        `<a class="btn btn-sm" href="${escHtml(rule.edit_search_url || '')}">Edit search ${icon('forward')}</a></div></div>`,
    }) : '') +
    fieldRow({
      // Stacked: the 220px control column cut "Each showing's default profile" short.
      label: 'Recording profile', stack: true,
      meta: 'Its padding is what keeps a program that runs long. Each showing\'s default is ' +
        'the profile its own Record button would pick.',
      control: '<select id="ar-profile"><option value="">Each showing\'s default profile</option>' +
        (opts.profiles || []).map((p) => `<option value="${escHtml(String(p.id))}"` +
          `${edit && p.id === rule.profile_id ? ' selected' : ''}>${escHtml(p.name)}</option>`).join('') +
        '</select>',
    }) +
    fieldRow({
      label: 'Skip repeats',
      meta: 'A showing whose title AND episode title both match an earlier airing that was ' +
        'already recorded is skipped. Showings without an episode title are never skipped ' +
        'as repeats.',
      control: `<label class="switch"><input type="checkbox" id="ar-repeats"${!edit || rule.skip_repeats ? ' checked' : ''}>` +
        '<span class="knob"></span></label>',
    }) +
    fieldRow({
      label: 'Limit', stack: true,
      meta: 'Optional - set both numbers or neither. The window is measured on start times, ' +
        'so it must be shorter than the shortest real gap between two events: a weekly ' +
        'event does not start at the same hour every week. For most series, leave it empty ' +
        'and let Skip repeats handle the replays.',
      control: '<div class="ar-limit">At most ' +
        `<input type="number" id="ar-limit-count" min="1" step="1" inputmode="numeric" aria-label="At most how many" value="${edit ? numberValue(rule.limit_count) : ''}">` +
        ' within ' +
        `<input type="number" id="ar-limit-hours" min="1" step="1" inputmode="numeric" aria-label="Within how many hours" value="${edit ? numberValue(rule.limit_window_hours) : ''}">` +
        ' hours</div>',
    }) +
    fieldRow({
      label: 'Cap on scheduled recordings',
      meta: 'The most recordings this rule may have scheduled at once; showings past it wait ' +
        `until earlier ones finish. Empty uses ${capDefault}; 0 means no cap.`,
      control: '<input type="number" id="ar-cap" min="0" step="1" inputmode="numeric" ' +
        `placeholder="${cap ? `Default (${nf(cap)})` : 'Default (no cap)'}" value="${edit ? numberValue(rule.max_upcoming) : ''}">`,
    }) +
    (edit ? '<p class="card-note">Saving keeps every recording this rule already scheduled, even ' +
      'one the new settings would not pick, and judges the showings it skipped again.</p>' : '');

  const $ = (sel) => body.querySelector(sel);
  const err = $('#ar-err');
  const fail = (message) => {
    err.textContent = message;
    err.hidden = false;
  };
  const verb = edit ? 'Save' : 'Create rule';

  const modal = buildModal({
    title: edit ? 'Edit rule' : 'Create rule from this search',
    panelClass: 'modal-wide',
    body,
    footer: [
      { label: 'Cancel', class: 'btn', onClick: (c) => c() },
      { label: verb, class: 'btn btn-primary', onClick: () => { submit(); return false; } },
    ],
  });
  const submitBtn = modal.querySelector('.modal-foot .btn-primary');

  const read = () => Object.assign(edit ? {} : { search: opts.search || '' }, {
    name: $('#ar-name').value.trim(),
    profile_id: $('#ar-profile').value ? Number($('#ar-profile').value) : null,
    skip_repeats: $('#ar-repeats').checked,
    limit_count: autoRuleNumber($('#ar-limit-count').value),
    limit_window_hours: autoRuleNumber($('#ar-limit-hours').value),
    max_upcoming: autoRuleNumber($('#ar-cap').value),
  });

  async function submit() {
    const payload = read();
    if (!payload.name) { fail('Give the rule a name.'); return; }
    if ((payload.limit_count === null) !== (payload.limit_window_hours === null)) {
      fail('Set both halves of "at most X within Y hours", or neither.');
      return;
    }
    err.hidden = true;
    submitBtn.disabled = true;
    submitBtn.textContent = edit ? 'Saving…' : 'Creating…';
    let res;
    try {
      res = edit
        ? await jsonFetch(`${opts.apiBase}${rule.id}`, { method: 'PUT', body: JSON.stringify(payload) })
        : await jsonFetch(opts.createUrl, { method: 'POST', body: JSON.stringify(payload) });
    } catch (e) {
      fail(e.message || 'The rule could not be saved.');
      submitBtn.disabled = false;
      submitBtn.textContent = verb;
      return;
    }
    modal.closeModal();
    if (edit && !res.evaluation) {
      // A paused rule is saved and not checked - which is what pausing means.
      showToast('Rule saved. It is paused, so nothing was checked.', { type: 'success' });
    } else if (edit) {
      const out = autoRuleOutcome('Rule saved.', res.evaluation, opts.phrases);
      showToast(out.text, { type: out.type, durationMs: 10000 });
    } else if (opts.goToRule && res.url) {
      const out = autoRuleOutcome('Rule saved.', res.evaluation, opts.phrases);
      showToastAfterLoad(out.text, { type: out.type, durationMs: 12000 });
      window.location.href = res.url;  // nav-ok: a rule made from + New rule opens its own page
      return;
    } else {
      const out = autoRuleOutcome('Rule saved.', res.evaluation, opts.phrases);
      const link = res.url
        ? `<a href="${escHtml(res.url)}">${escHtml(payload.name)} ${icon('forward')}</a>`
        : `<a href="${escHtml(opts.rulesUrl || '/rules')}">Rules ${icon('forward')}</a>`;
      showToast(`${escHtml(out.text)} ${link}`, { type: out.type, html: true, durationMs: 12000 });
    }
    if (opts.onSaved) opts.onSaved(res);
  }

  const first = $(edit ? (AUTO_RULE_FOCUS[opts.focus] || '#ar-name') : '#ar-name');
  first.focus();
  if (first.select && first.type !== 'checkbox' && first.tagName !== 'SELECT') first.select();
  return modal;
}

/* ── The actions every Rules surface shares ──────────────────────────────────────────
   `rule` is {id, name, scheduled} (scheduled = its SCHEDULED recordings); `cfg` is the
   page's config ({apiBase, phrases}); `onDone(res)` runs after the server said yes, so the
   page can redraw itself. A refusal is a toast and changes nothing on the page. */

async function autoRulePause(rule, cfg, onDone) {
  let res;
  try {
    res = await jsonFetch(`${cfg.apiBase}${rule.id}/pause`, { method: 'POST' });
  } catch (err) {
    showToast(err.message || 'The rule could not be paused.', { type: 'error' });
    return;
  }
  showToast(`Paused "${rule.name}". It schedules nothing new until you resume it.`
    + (rule.scheduled
      ? ` The ${_arCount(rule.scheduled, 'recording', 'recordings')} it already scheduled still record.`
      : ''));
  if (onDone) onDone(res);
}

async function autoRuleResume(rule, cfg, onDone) {
  let res;
  try {
    res = await jsonFetch(`${cfg.apiBase}${rule.id}/resume`, { method: 'POST' });
  } catch (err) {
    showToast(err.message || 'The rule could not be resumed.', { type: 'error' });
    return;
  }
  const out = autoRuleOutcome(`Resumed "${rule.name}".`, res && res.evaluation, cfg.phrases);
  showToast(out.text, { type: out.type, durationMs: 10000 });
  if (onDone) onDone(res);
}

async function autoRuleCheck(rule, cfg, onDone) {
  let res;
  try {
    res = await jsonFetch(`${cfg.apiBase}${rule.id}/check`, { method: 'POST' });
  } catch (err) {
    showToast(err.message || 'The rule could not be checked.', { type: 'error' });
    return;
  }
  const out = autoRuleOutcome(`Checked "${rule.name}".`, res && res.evaluation, cfg.phrases);
  showToast(out.text, { type: out.type, durationMs: 10000 });
  if (onDone) onDone(res);
}

function autoRuleDeletedText(res) {
  const cancelled = Number(res && res.cancelled) || 0;
  const kept = Number(res && res.kept) || 0;
  if (cancelled) {
    return `Rule deleted. ${_arCount(cancelled, 'scheduled recording', 'scheduled recordings')} cancelled.`
      + (kept ? ` ${_arCount(kept, 'other recording', 'other recordings')} kept.` : '');
  }
  if (kept) return `Rule deleted. ${_arCount(kept, 'recording', 'recordings')} kept as ordinary recordings.`;
  return 'Rule deleted.';
}

/* " 1 scheduled recording cancelled." for an edit's answer ({cancelled, kept}), with its
   leading space, or '' when the edit settled nothing - read after "Saved the new search". */
function autoRuleUnmatchedText(res) {
  const cancelled = Number(res && res.cancelled) || 0;
  const kept = Number(res && res.kept) || 0;
  return (cancelled ? ` ${_arCount(cancelled, 'scheduled recording', 'scheduled recordings')} cancelled.` : '')
    + (kept ? ` ${_arCount(kept, 'recording', 'recordings')} kept as `
      + `${kept === 1 ? 'an ordinary recording' : 'ordinary recordings'}.` : '');
}

/**
 * The question a changed search asks before it is saved (§8): the rule's scheduled
 * recordings the new search no longer matches, and whether to cancel them or keep them as
 * ordinary recordings.
 *   ruleName - the rule's name
 *   data     - the save's 409 body: {unmatched: [{id, name, when}], determined, reason}
 *   save(answer) - resends the save with answer = {action, ids}; resolves to the server's
 *              answer or throws. A 409 naming a new list redraws the dialog with it.
 *   onSaved(res) - after the save went through
 */
function openAutoRuleUnmatched(opts) {
  const choice = (value, checked, title, meta) =>
    `<label class="fp-radio"><input type="radio" name="ru-fate" value="${value}"${checked ? ' checked' : ''}>`
    + ` <strong>${escHtml(title)}</strong><span class="text-muted">${escHtml(meta)}</span></label>`;
  const body = document.createElement('div');
  let current = opts.data || {};

  const draw = (notice) => {
    const recs = current.unmatched || [];
    const n = recs.length;
    const them = n === 1 ? 'it' : 'them';
    const cancel = body.querySelector('input[name="ru-fate"][value="cancel"]');
    const cancelChosen = cancel ? cancel.checked : true;
    body.innerHTML =
      `<p class="modal-err" id="ru-err"${notice ? '' : ' hidden'}>${escHtml(notice || '')}</p>`
      + (current.determined === false
        ? `<p class="card-note">ChannelBin could not tell which of the ${_arCount(n, 'recording', 'recordings')} `
          + `"${escHtml(opts.ruleName)}" has scheduled the new search still matches, so all of them are listed. `
          + `${escHtml(current.reason || '')}</p>`
        : `<p class="card-note">The new search no longer matches ${_arCount(n, 'recording', 'recordings')} `
          + `"${escHtml(opts.ruleName)}" has scheduled:</p>`)
      + '<div class="statlist">'
      + recs.map((r) => `<div class="srow"><span class="sk">${escHtml(r.name)}</span>`
        + `<span class="sv">${escHtml(r.when || '')}</span></div>`).join('')
      + '</div>'
      + choice('cancel', cancelChosen,
        n === 1 ? 'Cancel it' : `Cancel the ${nf(n)} scheduled recordings`,
        `${n === 1 ? 'It is' : 'They are'} removed before ${n === 1 ? 'it starts' : 'they start'}.`)
      + choice('keep', !cancelChosen, `Keep ${them} as ordinary recordings`,
        `${n === 1 ? 'It still records' : 'They still record'} as scheduled, but no longer `
        + `${n === 1 ? 'belongs' : 'belong'} to the rule or ${n === 1 ? 'counts' : 'count'} toward its cap.`)
      + '<p class="card-note">Nothing that already started is touched.</p>';
  };
  draw('');

  const modal = buildModal({
    title: 'Save new search',
    body,
    footer: [
      { label: 'Cancel', class: 'btn', onClick: (c) => c() },
      { label: 'Save to rule', class: 'btn btn-primary', onClick: () => { submit(); return false; } },
    ],
  });
  const confirmBtn = modal.querySelector('.modal-foot .btn-primary');

  async function submit() {
    const picked = body.querySelector('input[name="ru-fate"]:checked');
    const answer = {
      action: picked ? picked.value : 'cancel',
      ids: (current.unmatched || []).map((r) => r.id),
    };
    confirmBtn.disabled = true;
    let res;
    try {
      res = await opts.save(answer);
    } catch (e) {
      confirmBtn.disabled = false;
      if (e.status === 409 && e.data && e.data.unmatched) {
        current = e.data;
        draw('The list changed while this was open. Check it again before saving.');
        return;
      }
      const err = body.querySelector('#ru-err');
      err.textContent = e.message || 'The search could not be saved to the rule.';
      err.hidden = false;
      return;
    }
    modal.closeModal();
    if (opts.onSaved) opts.onSaved(res);
  }
  return modal;
}

function openAutoRuleDelete(rule, cfg, onDone) {
  const n = rule.scheduled;
  const choice = (value, checked, title, meta) =>
    `<label class="fp-radio"><input type="radio" name="rd-fate" value="${value}"${checked ? ' checked' : ''}>`
    + ` <strong>${escHtml(title)}</strong><span class="text-muted">${escHtml(meta)}</span></label>`;
  const body = document.createElement('div');
  body.innerHTML =
    '<p class="modal-err" id="rd-err" hidden></p>'
    + `<p class="card-note">"${escHtml(rule.name)}" will be deleted, with its history of what `
    + 'it decided, and it schedules nothing more.</p>'
    + (n
      ? `<p class="card-note">It has ${_arCount(n, 'scheduled recording', 'scheduled recordings')} `
        + `that ${n === 1 ? 'has' : 'have'} not started yet.</p>`
        + choice('keep', true, 'Keep them as ordinary recordings',
          'They still record as scheduled; they just no longer belong to a rule.')
        + choice('cancel', false,
          `Cancel its ${_arCount(n, 'scheduled recording', 'scheduled recordings')}`,
          'They are removed before they start.')
      : '')
    + '<p class="card-note">Recordings it already started or finished are never touched.</p>';
  const err = body.querySelector('#rd-err');

  const modal = buildModal({
    title: 'Delete rule',
    body,
    footer: [
      { label: 'Cancel', class: 'btn', onClick: (c) => c() },
      { label: 'Delete rule', class: 'btn btn-danger', onClick: () => { submit(); return false; } },
    ],
  });
  const buttons = modal.querySelectorAll('.modal-foot .btn');
  const confirmBtn = buttons[buttons.length - 1];
  // A danger dialog starts on Cancel, so Enter cannot destroy (DESIGN.md 3.12).
  buttons[0].focus();

  async function submit() {
    const cancel = !!body.querySelector('input[name="rd-fate"][value="cancel"]:checked');
    confirmBtn.disabled = true;
    let res;
    try {
      res = await jsonFetch(`${cfg.apiBase}${rule.id}`, {
        method: 'DELETE', body: JSON.stringify({ cancel_scheduled: cancel }),
      });
    } catch (e) {
      err.textContent = e.message || 'The rule could not be deleted.';
      err.hidden = false;
      confirmBtn.disabled = false;
      return;
    }
    modal.closeModal();
    showToast(autoRuleDeletedText(res));
    if (onDone) onDone(res);
  }
  return modal;
}
