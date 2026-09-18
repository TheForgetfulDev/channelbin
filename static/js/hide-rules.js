/* Hide Rules page (dev/changelog/780). Built from the approved dev/mockups/35-hide-rules.html
   round 4 - the wizard/picker/preview shapes below are that mockup wired to the real API
   instead of a MOCK data blob.

   Two differences from the mockup, both because there is now a real backend:
   - Every mutation (add/edit/delete/toggle a rule, pick categories) is a real request
     against /api/channel-hide-rules, followed by a toast and a page reload (the
     jobs.js/tags.js convention: showToast, then location.reload() after enough time to
     read it) rather than the mockup's in-memory RULES array. That keeps this file from
     ever needing to re-derive `Channel.hidden`/`hidden_deferred` client-side, which
     app/channel_hiding.py already computes as one indexed column, not something a
     136,940-channel table's worth of GLOB matching should be re-run for in a browser.
   - A materialize refusal (the save succeeded but the rule pass was refused, e.g. by
     database contention) is surfaced by the page itself: hide_rules_page() hands the
     pending retry down as `pending_materialize` and the template paints a banner naming
     the blocker and when it will retry (scheduler.py::pending_hide_materialize). The
     toasts below point at that banner rather than restating the refusal, because the
     reload every mutation already does is what paints it (dev/changelog/928).

   The summary tiles (#hr-summary-body) are server-rendered from real Channel.hidden /
   hidden_deferred aggregates - they are not recomputed here, and a reload is what keeps
   them fresh after a mutation. The rules table and the account filter ARE rendered here,
   from the rules/accounts snapshot embedded in window.HIDE_RULES_CONFIG at page load -
   the same "render the collection in JS from an embedded config blob" shape
   static/js/channel-search.js already uses for its rail. */

const CFG = window.HIDE_RULES_CONFIG;
const API_BASE = '/api/channel-hide-rules';

const RULES = CFG.rules;          // page-load snapshot; a mutation reloads the page rather
                                   // than patching this, so it never goes stale mid-session
let selectedAccounts = new Set(); // string keys: 'global' | '<account id>'. Empty = no filter.
let activeWizardOverlay = null;
let lastWizKind = 'category';     // remembered across a Back navigation within one wizard session
let lastCatMode = 'pick';
let categoriesCache = null;       // all categories, every account - fetched once, reused
                                   // across every time the picker is opened this page load

function accountById(id) { return CFG.accounts.find(a => a.id === id) || null; }
function accountLabel(accountId) { return accountId == null ? 'All' : ((accountById(accountId) || {}).name || `Account ${accountId}`); }
function accKey(accountId) { return accountId == null ? 'global' : String(accountId); }

function reloadAfter(message, type) {
  showToast(message, type ? { type } : undefined);
  setTimeout(() => window.location.reload(), 1500);
}

// A save/edit/delete succeeded; `data` is the API response ({success, rule, materialized,
// refusal?}). materialized:false means the rule is saved but not yet applied - the banner
// the reload paints names the blocker and when it retries, so this only has to point at it.
function afterSave(data, successMessage) {
  if (data.materialized === false) {
    reloadAfter('Saved, but not applied to your channels yet - see the banner for why. It will retry automatically.', 'warning');
  } else {
    reloadAfter(successMessage, 'success');
  }
}

// ── Account filter (multi-select), scoped to the rules list ────────────────────────────
function accountFilterOptions() {
  return [{ v: 'global', label: 'All' }].concat(CFG.accounts.map(a => ({ v: String(a.id), label: a.name })));
}

function toggleAccountFilter(key) {
  if (selectedAccounts.has(key)) selectedAccounts.delete(key); else selectedAccounts.add(key);
  renderAll();
}

function renderScopeControl() {
  const pillsWrap = document.getElementById('scope-pills');
  const opts = accountFilterOptions();
  pillsWrap.innerHTML = opts.map(o => `<button type="button" class="scope-pill${selectedAccounts.has(o.v) ? ' active' : ''}" data-scope="${o.v}">${escHtml(o.label)}</button>`).join('');
  pillsWrap.querySelectorAll('.scope-pill').forEach(btn => btn.addEventListener('click', () => toggleAccountFilter(btn.dataset.scope)));
}

function defaultAccountForForm() {
  const specific = Array.from(selectedAccounts).filter(v => v !== 'global');
  if (specific.length === 1) return parseInt(specific[0], 10);
  return null;
}

// ── Generic sort helpers, shared by the rules table and the category picker table ──────
function sortBy(rows, keyFn, dir) {
  const mul = dir === 'desc' ? -1 : 1;
  return rows.slice().sort((a, b) => {
    const ka = keyFn(a), kb = keyFn(b);
    if (ka < kb) return -1 * mul;
    if (ka > kb) return 1 * mul;
    return 0;
  });
}
function sortTh(col, label, sortState, extraClass) {
  const active = sortState.col === col;
  const dirClass = active && sortState.dir === 'desc' ? ' desc' : '';
  return `<th class="sortable${extraClass ? ' ' + extraClass : ''}" data-sortcol="${col}">${escHtml(label)}<span class="sort-caret${active ? ' active' : ''}${dirClass}">▾</span></th>`;
}
function wireSortHeaders(root, sortState, onSort) {
  root.querySelectorAll('th.sortable').forEach(th => th.addEventListener('click', () => {
    const col = th.dataset.sortcol;
    if (sortState.col === col) sortState.dir = sortState.dir === 'asc' ? 'desc' : 'asc';
    else { sortState.col = col; sortState.dir = 'asc'; }
    onSort();
  }));
}

// ── Rules list ───────────────────────────────────────────────────────────────────
let ruleSort = { col: 'target', dir: 'asc' };
const RULE_SORT_KEYS = {
  target: r => CFG.targetLabels[r.target] || r.target,
  pattern: r => r.pattern.toLowerCase(),
  account: r => accountLabel(r.account_id).toLowerCase(),
  matches: r => r.match_count,
  on: r => r.enabled ? 1 : 0,
};

function rulesForSelection() {
  if (selectedAccounts.size === 0) return RULES.slice();
  return RULES.filter(r => selectedAccounts.has(accKey(r.account_id)));
}

function ruleRowHtml(r) {
  const scopeTotal = r.account_id ? (accountById(r.account_id) || {}).channel_count || 0 : CFG.totalChannels;
  const deferredHtml = r.deferred_count > 0
    ? `<span class="rule-deferred-note" data-tip="${r.deferred_count} of these are in your TV Guide or a channel group, so they stay visible until you remove them from there.">+${nf(r.deferred_count)} kept visible</span>`
    : '';
  const menuItems = r.target === 'category_exact'
    ? `<button class="menu-item danger" data-act="delete-rule" data-id="${r.id}">Delete rule</button>`
    : `<button class="menu-item" data-act="edit-rule" data-id="${r.id}">Edit</button><div class="sep"></div><button class="menu-item danger" data-act="delete-rule" data-id="${r.id}">Delete rule</button>`;
  return `<tr class="${r.enabled ? '' : 'rule-disabled-row'}" data-rule-id="${r.id}">
    <td data-label="Type"><span class="rule-target-badge t-${r.target}">${escHtml(CFG.targetLabels[r.target] || r.target)}</span></td>
    <td data-label="Pattern" class="rule-pattern">${escHtml(r.pattern)}</td>
    <td data-label="Account"><span class="rule-scope-badge">${escHtml(accountLabel(r.account_id))}</span></td>
    <td data-label="Matches" class="num"><span class="rule-match-n" data-tip="${nf(r.match_count)} of ${nf(scopeTotal)} channels in scope">${nf(r.match_count)}</span>${deferredHtml}</td>
    <td data-label="On"><label class="switch" data-tip="${r.enabled ? 'On - this rule is being applied' : 'Off - kept for later, not applied right now'}"><input type="checkbox" ${r.enabled ? 'checked' : ''} data-act="toggle-rule" data-id="${r.id}"><span class="knob"></span></label></td>
    <td style="text-align:right"><span class="menu-wrap"><button class="btn btn-sm btn-icon" data-menu aria-label="More actions for this rule">&#8943;</button><div class="menu pop-left">${menuItems}</div></span></td>
  </tr>`;
}

function ruleTableHtml(rows, emptyText) {
  if (!rows.length) return `<div class="rules-empty">${emptyText}</div>`;
  const sorted = sortBy(rows, RULE_SORT_KEYS[ruleSort.col] || RULE_SORT_KEYS.target, ruleSort.dir);
  // The rules table opts into the phone card reflow; the category picker and the preview
  // table below do not - both live inside a modal, already constrained by the sheet.
  // Every <td> in ruleRowHtml therefore carries a data-label (style.css, `.tbl-cards`).
  return `<div class="table-scroll"><table class="tbl tbl-cards">
    <thead><tr>
      ${sortTh('target', 'Type', ruleSort)}
      ${sortTh('pattern', 'Pattern', ruleSort)}
      ${sortTh('account', 'Account', ruleSort)}
      ${sortTh('matches', 'Matches', ruleSort, 'num')}
      ${sortTh('on', 'On', ruleSort)}
      <th></th>
    </tr></thead>
    <tbody>${sorted.map(r => ruleRowHtml(r)).join('')}</tbody>
  </table></div>`;
}

function renderRulesSection() {
  const scoped = rulesForSelection();
  const el = document.getElementById('rules-section');
  el.innerHTML = `<div class="card">
    <div class="card-head"><h2>Rules <span class="cnt">${scoped.length}</span></h2></div>
    <div class="card-body">${ruleTableHtml(scoped, 'No rules match this filter yet. Click + Add Rule to create one.')}</div>
  </div>`;
  wireSortHeaders(el, ruleSort, () => renderRulesSection());
  el.querySelectorAll('[data-act="toggle-rule"]').forEach(cb => cb.addEventListener('change', () => {
    const enabled = cb.checked;
    jsonFetch(`${API_BASE}/${cb.dataset.id}`, { method: 'PATCH', body: JSON.stringify({ enabled }) })
      .then((data) => afterSave(data, enabled
        ? `Rule turned on. Re-applying to ${nf(data.rule.match_count)} channels...`
        : 'Rule turned off. Channels it matched may become visible again.'))
      .catch((err) => {
        cb.checked = !enabled;   // put the knob back where the server has it
        showToast(err.message || 'Could not change that rule.', { type: 'error' });
      });
  }));
  el.querySelectorAll('[data-act="edit-rule"]').forEach(btn => btn.addEventListener('click', () => openPatternForm(RULES.find(x => x.id === parseInt(btn.dataset.id, 10)))));
  el.querySelectorAll('[data-act="delete-rule"]').forEach(btn => btn.addEventListener('click', () => confirmDeleteRule(parseInt(btn.dataset.id, 10))));
}

function confirmDeleteRule(id) {
  const r = RULES.find(x => x.id === id);
  if (!r) return;
  buildModal({
    title: 'Delete rule',
    body: `<p>Delete the rule <span class="rule-pattern">${escHtml(r.pattern)}</span>?</p>
           <p style="color:var(--text-muted);font-size:var(--fs-md);">Channels it currently hides become visible again, unless another rule or your own hide still covers them.</p>`,
    footer: [
      { label: 'Cancel', class: 'btn' },
      { label: 'Delete rule', class: 'btn btn-danger', onClick: (close) => {
        jsonFetch(`${API_BASE}/${id}`, { method: 'DELETE' })
          .then((data) => { close(); afterSave(data, 'Rule deleted. Re-checking affected channels...'); })
          .catch((err) => showToast(err.message || 'Could not delete that rule.', { type: 'error' }));
        return false;
      } },
    ],
  });
}

// ── Add rule: what do you want to hide? (mirrors clone-modal.js's openCloneTypeModal
// step-1/step-2 shape - a scope choice first, the target-specific screen second) ───────
function openAddRuleWizard() {
  formState = null;
  picker = null;
  lastWizKind = 'category';
  lastCatMode = 'pick';
  openWizStepKind();
}

function typeChoiceHtml(name, options, current) {
  return `<div class="clone-type-choice">${options.map(o => {
    const sel = current === o.value ? ' sel' : '';
    return `<label class="clone-type-opt${sel}">
      <input type="radio" name="${name}" value="${o.value}"${current === o.value ? ' checked' : ''}>
      <strong>${escHtml(o.title)}</strong><br><span class="text-muted small">${escHtml(o.meta)}</span></label>`;
  }).join('')}</div>`;
}

function openWizStepKind() {
  const state = { kind: lastWizKind };
  const body = document.createElement('div');
  function render() {
    body.innerHTML = typeChoiceHtml('hr-kind', [
      { value: 'category', title: 'Provider categories', meta: 'Hide all channels found in one or more categories.' },
      { value: 'channel', title: 'Channel names', meta: 'Hide channels that match a pattern, e.g. everything starting with "24/7".' },
    ], state.kind);
  }
  body.addEventListener('change', (e) => { if (e.target.name === 'hr-kind') { state.kind = e.target.value; render(); } });
  const modal = buildModal({
    title: 'Add rule - what do you want to hide?',
    body,
    onClose: () => { activeWizardOverlay = null; },
    footer: [
      { label: 'Cancel', class: 'btn' },
      { label: 'Continue', class: 'btn btn-primary', onClick: (close) => {
        close();
        lastWizKind = state.kind;
        if (state.kind === 'channel') {
          openPatternForm(null, { target: 'name_glob', account_id: defaultAccountForForm(), onBack: openWizStepKind });
        } else {
          openCategoryModeChoice(openWizStepKind);
        }
        return false;
      } },
    ],
  });
  activeWizardOverlay = modal;
  render();
}

function openCategoryModeChoice(onBack) {
  const state = { mode: lastCatMode };
  const body = document.createElement('div');
  function render() {
    body.innerHTML = typeChoiceHtml('hr-catmode', [
      { value: 'pick', title: 'Pick specific categories', meta: 'Browse your provider categories and select the ones to hide.' },
      { value: 'pattern', title: 'Match with a pattern', meta: 'Hide every category whose name matches a pattern, e.g. "*Sports*".' },
    ], state.mode);
  }
  body.addEventListener('change', (e) => { if (e.target.name === 'hr-catmode') { state.mode = e.target.value; render(); } });
  const modal = buildModal({
    title: 'Add rule - provider categories',
    body,
    onClose: () => { activeWizardOverlay = null; },
    footer: [
      { label: 'Back', class: 'btn', onClick: (close) => { close(); onBack(); return false; } },
      { label: 'Cancel', class: 'btn' },
      { label: 'Continue', class: 'btn btn-primary', onClick: (close) => {
        close();
        lastCatMode = state.mode;
        if (state.mode === 'pick') openCategoryPicker(() => openCategoryModeChoice(onBack));
        else openPatternForm(null, { target: 'category_glob', account_id: defaultAccountForForm(), onBack: () => openCategoryModeChoice(onBack) });
        return false;
      } },
    ],
  });
  activeWizardOverlay = modal;
  render();
}

// ── Pattern form (channel-name path, and the category "match with a pattern" leaf) ─────
let formState = null;   // { editingId, target, account_id, pattern, confirmed }
let previewTimer = null;
let previewSeq = 0;     // ignore a stale preview response overtaken by a newer keystroke

function patternFormBodyHtml() {
  const accOpts = [`<option value="">All accounts</option>`]
    .concat(CFG.accounts.map(a => `<option value="${a.id}" ${formState.account_id === a.id ? 'selected' : ''}>${escHtml(a.name)}</option>`));
  const kindLabel = formState.target === 'name_glob' ? 'Channel name pattern' : 'Category pattern';
  const placeholder = formState.target === 'name_glob' ? 'e.g. 24/7* or *TEST*' : 'e.g. *Sports* or *Movies*';
  return `
    <div class="preview-section-t" style="margin:0 0 10px;">${escHtml(kindLabel)}</div>
    <div class="rule-form-row">
      <label><span class="fl-label">Account</span>
        <select id="rf-account">${accOpts.join('')}</select>
      </label>
      <label><span class="fl-label">Pattern</span>
        <input type="text" id="rf-pattern" class="mono" autocomplete="off" spellcheck="false"
          placeholder="${placeholder}" value="${escHtml(formState.pattern)}">
      </label>
    </div>
    <div class="preview-section-t">What this WILL hide</div>
    <div class="preview-box" id="rf-preview"></div>`;
}

function wireFormInputs(onChange) {
  document.getElementById('rf-account').addEventListener('change', (e) => {
    formState.account_id = e.target.value ? parseInt(e.target.value, 10) : null;
    formState.confirmed = false;
    onChange();
  });
  const patternInput = document.getElementById('rf-pattern');
  patternInput.addEventListener('input', (e) => {
    formState.pattern = e.target.value;
    formState.confirmed = false;
    clearTimeout(previewTimer);
    document.getElementById('rf-preview').innerHTML = '<span class="preview-spin"></span><span class="preview-hint">Checking...</span>';
    previewTimer = setTimeout(onChange, 300);
  });
  setTimeout(() => patternInput.focus(), 30);
}

function sampleTableHtml(items, colLabel, matchedTotal) {
  if (!items.length) return '';
  const shown = items.slice(0, 20);
  const moreRow = matchedTotal > shown.length
    ? `<tr><td class="text-faint">+${nf(matchedTotal - shown.length)} more</td></tr>` : '';
  return `<div class="preview-table-wrap table-scroll"><table class="tbl">
    <thead><tr><th>${escHtml(colLabel)}</th></tr></thead>
    <tbody>${shown.map(n => `<tr><td>${escHtml(n)}</td></tr>`).join('')}${moreRow}</tbody>
  </table></div>`;
}

// Fetches the live preview for the form's current target/account/pattern (debounced by the
// caller) and renders it. `p` is handed to onConfirmChange so the Save button can read
// hides_everything/duplicate state without a second round trip.
function renderPreview(onConfirmChange) {
  const el = document.getElementById('rf-preview');
  if (!el) return;
  if (!formState.pattern) { el.innerHTML = '<span class="preview-hint">Type a pattern to see what it would hide.</span>'; onConfirmChange(null); return; }
  const seq = ++previewSeq;
  jsonFetch(`${API_BASE}/preview`, { method: 'POST', body: JSON.stringify({
    target: formState.target, account_id: formState.account_id, pattern: formState.pattern }) })
    .then((data) => { if (seq === previewSeq) renderPreviewResult(data, onConfirmChange); })
    .catch((err) => {
      if (seq !== previewSeq) return;
      el.innerHTML = `<span class="preview-hint">${escHtml(err.message || 'That pattern is not valid.')}</span>`;
      onConfirmChange(null);
    });
}

function renderPreviewResult(p, onConfirmChange) {
  const el = document.getElementById('rf-preview');
  if (!el) return;
  const scopeName = formState.account_id ? accountLabel(formState.account_id) : 'all accounts';
  const deferredHtml = p.deferred ? ` <span style="color:var(--warn)">(${nf(p.deferred)} kept visible)</span>` : '';
  let tablesHtml = '';
  if (p.sample && p.sample.length) {
    tablesHtml += `<div class="preview-section-t" style="margin-top:10px">Sample channels</div>${sampleTableHtml(p.sample.map(s => s.name), 'Channel name', p.matched)}`;
  }
  if (p.categories && p.categories.length) {
    tablesHtml += `<div class="preview-section-t">Categories caught</div>${sampleTableHtml(p.categories, 'Category', p.categories.length)}`;
  }
  let warnHtml = '';
  if (p.hides_everything) {
    warnHtml = `<div class="warn-box">This pattern matches every channel in scope - all ${nf(p.matched)} of them.
      <label><input type="checkbox" id="rf-confirm" ${formState.confirmed ? 'checked' : ''}> I understand - hide all ${nf(p.matched)} channels anyway.</label>
    </div>`;
  }
  // A duplicate is a hard stop (no override), so it is named right under the headline rather
  // than only in the Save button's hover title - a disabled button with no visible reason is
  // exactly the "number the user can't explain" CLAUDE.md's principle 1 rules out.
  let dupHtml = '';
  if (duplicateRuleExists()) {
    dupHtml = `<div class="dup-box">A rule for this exact pattern already exists for ${escHtml(scopeName)}. Edit or delete it from the Rules list instead of adding it again.</div>`;
  }
  el.innerHTML = `<div class="preview-headline"><span class="n">${nf(p.matched)}</span> matched in ${escHtml(scopeName)} <span style="color:var(--text-faint);font-weight:400;">(${nf(p.scope_total)} channels)</span>${deferredHtml}</div>${dupHtml}${tablesHtml}${warnHtml}`;
  const confirmBox = document.getElementById('rf-confirm');
  if (confirmBox) confirmBox.addEventListener('change', (e) => { formState.confirmed = e.target.checked; onConfirmChange(p); });
  onConfirmChange(p);
}

function duplicateRuleExists() {
  return RULES.some(r => r.id !== formState.editingId && r.target === formState.target
    && r.account_id === formState.account_id && r.pattern === formState.pattern);
}

function openPatternForm(existingRule, opts) {
  opts = opts || {};
  formState = existingRule
    ? { editingId: existingRule.id, target: existingRule.target, account_id: existingRule.account_id, pattern: existingRule.pattern, confirmed: false }
    : { editingId: null, target: opts.target || 'name_glob',
        account_id: ('account_id' in opts) ? opts.account_id : defaultAccountForForm(),
        pattern: opts.pattern || '', confirmed: false };
  const isEdit = formState.editingId != null;
  const footer = [];
  if (opts.onBack && !isEdit) footer.push({ label: 'Back', class: 'btn', onClick: (close) => { close(); opts.onBack(); return false; } });
  footer.push({ label: 'Cancel', class: 'btn' });
  footer.push({ label: isEdit ? 'Save' : 'Save rule', class: 'btn btn-primary', onClick: (close) => trySaveForm(close) });

  const modal = buildModal({
    title: isEdit ? 'Edit rule' : (formState.target === 'name_glob' ? 'Add rule - channel name pattern' : 'Add rule - category pattern'),
    body: patternFormBodyHtml(),
    panelClass: 'modal-wide',
    onClose: () => { activeWizardOverlay = null; },
    footer,
  });
  activeWizardOverlay = modal;
  const saveBtn = modal.querySelector('.modal-foot .btn-primary');
  const updateSaveState = (p) => {
    const dup = duplicateRuleExists();
    saveBtn.disabled = !formState.pattern || dup || (p && p.hides_everything && !formState.confirmed);
    saveBtn.title = dup ? 'That rule already exists.' : '';
  };
  wireFormInputs(() => renderPreview(updateSaveState));
  renderPreview(updateSaveState);
}

// Always returns false: closing is this function's own job (only on a successful save),
// never buildModal's automatic one, so a failed save leaves the form open to fix and retry.
function trySaveForm(closeFn) {
  if (!formState.pattern) return false;
  if (duplicateRuleExists()) { showToast('That rule already exists.', { type: 'error' }); return false; }
  const isEdit = formState.editingId != null;
  const payload = { target: formState.target, account_id: formState.account_id, pattern: formState.pattern };
  if (formState.confirmed) payload.confirm = true;
  const url = isEdit ? `${API_BASE}/${formState.editingId}` : API_BASE;
  jsonFetch(url, { method: isEdit ? 'PATCH' : 'POST', body: JSON.stringify(payload) })
    .then((data) => {
      closeFn();
      afterSave(data, isEdit ? 'Rule updated.' : `Rule saved. Re-applying to ${nf(data.preview ? data.preview.matched : 0)} channels...`);
    })
    .catch((err) => showToast(err.message || 'Could not save that rule.', { type: 'error' }));
  return false;
}

// ── Category picker (the "pick specific categories" leaf) - Provider Categories,
// sortable, filterable by account, and switchable between a flat list and groups ───────
let picker = null;   // { onBack, search, sort: {col, dir}, picked: Set<'accountId|name'>, view,
                     //   accounts: Set<accountId string>, openGroups: Set<prefix string>, rows }
let pickerAfterChange = () => {};

function hasWildcardChars(text) { return /[*?[]/.test(text); }

// SQLite GLOB -> RegExp, the JS counterpart to app/channel_hiding.py's use of SQLite's own
// GLOB operator: * = any run, ? = one char, [...] = a character class, case-sensitive,
// anchored to the whole string. Used only for the picker's client-side search box, which
// filters an already-fetched category list - never for deciding what a rule matches, which
// is always the server's answer.
function globToRegExp(pattern) {
  let out = '';
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i];
    if (c === '*') { out += '.*'; continue; }
    if (c === '?') { out += '.'; continue; }
    if (c === '[') {
      let j = i + 1, cls = '[';
      if (pattern[j] === '^' || pattern[j] === '!') { cls += '^'; j++; }
      while (j < pattern.length && pattern[j] !== ']') { cls += pattern[j]; j++; }
      cls += ']';
      out += cls;
      i = j;
      continue;
    }
    out += c.replace(/[.*+^${}()|[\]\\]/g, '\\$&');
  }
  return new RegExp('^' + out + '$');
}

function categoryMatchesSearch(name, text) {
  if (!text) return true;
  if (hasWildcardChars(text)) {
    try { return globToRegExp(text).test(name); } catch (e) { /* fall through */ }
  }
  return name.toLowerCase().includes(text.toLowerCase());
}

function prefixOf(name) {
  const m = name.match(/^([^|:]{1,14}[|:]\s?)/);
  if (m) return m[1];
  const w = name.trim().split(/\s+/)[0];
  return w || '(other)';
}

function loadCategories() {
  if (categoriesCache) return Promise.resolve(categoriesCache);
  return jsonFetch(`${API_BASE}/categories`).then((data) => { categoriesCache = data.categories; return categoriesCache; });
}

function pickerRows() {
  let rows = picker.rows;
  if (picker.accounts.size) rows = rows.filter(r => picker.accounts.has(String(r.account_id)));
  if (picker.search) rows = rows.filter(r => categoryMatchesSearch(r.category_name, picker.search));
  return rows;
}

function pickerRowKey(row) { return `${row.account_id}|${row.category_name}`; }
function splitPickerKey(key) {
  const idx = key.indexOf('|');
  return [key.slice(0, idx), key.slice(idx + 1)];
}

function pickerRowStatus(row) {
  const exact = RULES.find(r => r.target === 'category_exact' && r.account_id === row.account_id && r.pattern === row.category_name);
  if (exact) return { state: 'existing' };
  const glob = RULES.find(r => r.target === 'category_glob' && (r.account_id === null || r.account_id === row.account_id)
    && (() => { try { return globToRegExp(r.pattern).test(row.category_name); } catch (e) { return false; } })());
  if (glob) return { state: 'covered', rule: glob };
  return { state: 'free' };
}

const CAT_SORT_KEYS = {
  category_name: r => r.category_name.toLowerCase(),
  account: r => accountLabel(r.account_id).toLowerCase(),
  channel_count: r => r.channel_count,
};

function pickerRowHtml(row) {
  const status = pickerRowStatus(row);
  const key = pickerRowKey(row);
  let checkboxHtml;
  if (status.state === 'covered') {
    checkboxHtml = `<input type="checkbox" checked disabled data-tip="Already hidden by the pattern rule '${escHtml(status.rule.pattern)}'. Edit or delete that rule to change it.">`;
  } else if (status.state === 'existing') {
    checkboxHtml = `<input type="checkbox" checked disabled data-tip="Already added as its own rule. Delete it from the Rules list to remove.">`;
  } else {
    checkboxHtml = `<input type="checkbox" ${picker.picked.has(key) ? 'checked' : ''} data-act="pick-cat" data-key="${escHtml(key)}">`;
  }
  return `<tr>
    <td class="cat-check-cell">${checkboxHtml}</td>
    <td class="cat-name-cell mono" title="${escHtml(row.category_name)}">${escHtml(row.category_name)}</td>
    <td class="muted small">${escHtml(accountLabel(row.account_id))}</td>
    <td class="num">${nf(row.channel_count)}</td>
  </tr>`;
}

function categoryPickerTableHtml(rows) {
  if (!rows.length) return '<div class="cat-empty">No categories match that search.</div>';
  const sorted = sortBy(rows, CAT_SORT_KEYS[picker.sort.col] || CAT_SORT_KEYS.category_name, picker.sort.dir);
  return `<div class="table-scroll cat-list-scroll"><table class="tbl">
    <thead><tr>
      <th></th>
      ${sortTh('category_name', 'Category', picker.sort)}
      ${sortTh('account', 'Account', picker.sort)}
      ${sortTh('channel_count', 'Channels', picker.sort, 'num')}
    </tr></thead>
    <tbody>${sorted.map(pickerRowHtml).join('')}</tbody>
  </table></div>`;
}

function categoryPickerGroupedHtml(rows) {
  const groups = new Map();
  for (const row of rows) {
    const p = prefixOf(row.category_name);
    if (!groups.has(p)) groups.set(p, []);
    groups.get(p).push(row);
  }
  const ordered = Array.from(groups.entries()).sort((a, b) => b[1].length - a[1].length);
  return `<div>${ordered.map(([prefix, grows], i) => {
    const total = grows.reduce((s, r) => s + r.channel_count, 0);
    const gid = 'pg' + i;
    const isOpen = picker.openGroups.has(prefix);
    return `<div class="cat-group${isOpen ? ' open' : ''}" id="${gid}">
      <div class="cat-group-head" data-toggle="${gid}" data-prefix="${escHtml(prefix)}">
        <span class="cat-group-caret">&#9654;</span>
        <span class="cat-group-name">${escHtml(prefix.trim() || '(other)')}</span>
        <span class="cat-group-meta">${grows.length} categor${grows.length === 1 ? 'y' : 'ies'} &middot; ${nf(total)} channels</span>
        <button class="btn btn-sm" data-hideall="${escHtml(prefix)}">Hide all &rarr;</button>
      </div>
      <div class="cat-group-body">${categoryPickerTableHtml(grows)}</div>
    </div>`;
  }).join('')}</div>`;
}

function pickerToolbarHtml() {
  return `<div class="picker-toolbar">
    <input class="search cat-search" id="pk-search" type="text" placeholder="Search categories - try a pattern like *Sports*" value="${escHtml(picker.search)}">
    <button class="btn btn-sm" id="pk-select-all" type="button">Select all visible</button>
    <button class="btn btn-sm" id="pk-clear" type="button">Clear selection</button>
    <span class="picked-count">${picker.picked.size} selected</span>
  </div>`;
}

// Flat is the default view.
function pickerViewToggleHtml() {
  return `<div class="seg-toggle" id="pk-view-toggle">
    <button type="button" class="seg-btn${picker.view === 'flat' ? ' active' : ''}" data-view="flat">Flat list</button>
    <button type="button" class="seg-btn${picker.view === 'grouped' ? ' active' : ''}" data-view="grouped">Grouped</button>
  </div>`;
}

function pickerAccountFilterHtml() {
  const opts = CFG.accounts.map(a => ({ v: String(a.id), label: a.name }));
  return `<div class="scope-row picker-scope-row">
    <div class="scope-label">Account</div>
    <div class="scope-pills">${opts.map(o => `<button type="button" class="scope-pill${picker.accounts.has(o.v) ? ' active' : ''}" data-pkacc="${o.v}">${escHtml(o.label)}</button>`).join('')}</div>
  </div>`;
}

function categoryPickerBodyHtml() {
  return `${pickerViewToggleHtml()}${pickerAccountFilterHtml()}${pickerToolbarHtml()}<div id="pk-table-wrap"></div>`;
}

function wirePickerViewToggle() {
  document.querySelectorAll('#pk-view-toggle .seg-btn').forEach(btn => btn.addEventListener('click', () => {
    picker.view = btn.dataset.view;
    document.querySelectorAll('#pk-view-toggle .seg-btn').forEach(b => b.classList.toggle('active', b.dataset.view === picker.view));
    rerenderPickerTable();
  }));
}

function wirePickerAccountFilter() {
  document.querySelectorAll('.picker-scope-row .scope-pill').forEach(btn => btn.addEventListener('click', () => {
    const v = btn.dataset.pkacc;
    if (picker.accounts.has(v)) picker.accounts.delete(v); else picker.accounts.add(v);
    btn.classList.toggle('active');
    rerenderPickerTable();
  }));
}

function pickSaveLabel() { return picker.picked.size ? `Save ${picker.picked.size} rule${picker.picked.size === 1 ? '' : 's'}` : 'Save'; }

function wirePickerToolbar(afterChange) {
  pickerAfterChange = afterChange;
  document.getElementById('pk-search').addEventListener('input', (e) => {
    picker.search = e.target.value;
    rerenderPickerTable();
  });
  document.getElementById('pk-select-all').addEventListener('click', () => {
    pickerRows().forEach(row => { if (pickerRowStatus(row).state === 'free') picker.picked.add(pickerRowKey(row)); });
    rerenderPickerTable();
  });
  document.getElementById('pk-clear').addEventListener('click', () => {
    picker.picked.clear();
    rerenderPickerTable();
  });
}

function wirePickerTableEvents() {
  const root = document.getElementById('pk-table-wrap');
  wireSortHeaders(root, picker.sort, () => rerenderPickerTable());
  root.querySelectorAll('[data-act="pick-cat"]').forEach(cb => cb.addEventListener('change', () => {
    if (cb.checked) picker.picked.add(cb.dataset.key); else picker.picked.delete(cb.dataset.key);
    rerenderPickerTable();
  }));
  root.querySelectorAll('[data-toggle]').forEach(h => h.addEventListener('click', (e) => {
    if (e.target.closest('[data-hideall]')) return;
    const prefix = h.dataset.prefix;
    if (picker.openGroups.has(prefix)) picker.openGroups.delete(prefix); else picker.openGroups.add(prefix);
    document.getElementById(h.dataset.toggle).classList.toggle('open');
  }));
  root.querySelectorAll('[data-hideall]').forEach(b => b.addEventListener('click', (e) => {
    e.stopPropagation();
    const prefix = b.dataset.hideall;
    if (activeWizardOverlay) activeWizardOverlay.closeModal();
    openPatternForm(null, { target: 'category_glob', account_id: null, pattern: prefix + '*' });
  }));
}

function rerenderPickerTable() {
  const rows = pickerRows();
  document.getElementById('pk-table-wrap').innerHTML = picker.view === 'grouped'
    ? categoryPickerGroupedHtml(rows) : categoryPickerTableHtml(rows);
  wirePickerTableEvents();
  const countEl = document.querySelector('.picked-count');
  if (countEl) countEl.textContent = `${picker.picked.size} selected`;
  pickerAfterChange();
}

// Closes immediately (matching the bulk-action convention elsewhere in the app) and reports
// once every request settles. Independent POSTs rather than one batch call: the API creates
// one rule at a time, and Promise.allSettled means one category that turns out to already be
// a duplicate does not stop the rest of the picked selection from saving.
function savePickerSelections(close) {
  if (!picker.picked.size) return false;
  const keys = Array.from(picker.picked);
  const reqs = keys.map((key) => {
    const [accIdStr, name] = splitPickerKey(key);
    return jsonFetch(API_BASE, { method: 'POST', body: JSON.stringify({
      target: 'category_exact', account_id: parseInt(accIdStr, 10), pattern: name }) });
  });
  close();
  Promise.allSettled(reqs).then((results) => {
    const ok = results.filter(r => r.status === 'fulfilled').length;
    const failed = results.length - ok;
    const anyRefused = results.some(r => r.status === 'fulfilled' && r.value.materialized === false);
    if (!ok) { showToast('Could not save any of those categories.', { type: 'error' }); return; }
    const msg = `${ok} categor${ok === 1 ? 'y' : 'ies'} hidden.` + (failed ? ` ${failed} could not be saved.` : '');
    if (anyRefused) reloadAfter(`${msg} Some rules were not applied to your channels yet - see the banner for why.`, 'warning');
    else reloadAfter(msg, failed ? 'warning' : 'success');
  });
  return true;
}

function openCategoryPicker(onBack) {
  picker = { onBack, search: '', sort: { col: 'category_name', dir: 'asc' }, picked: new Set(), view: 'flat', accounts: new Set(), openGroups: new Set(), rows: [] };
  const footer = [];
  if (onBack) footer.push({ label: 'Back', class: 'btn', onClick: (close) => { close(); onBack(); return false; } });
  footer.push({ label: 'Cancel', class: 'btn' });
  footer.push({ label: 'Save', class: 'btn btn-primary', onClick: (close) => savePickerSelections(close) });
  const modal = buildModal({
    title: 'Add rule - pick categories',
    body: '<span class="preview-spin"></span><span class="preview-hint">Loading categories...</span>',
    panelClass: 'modal-wide',
    onClose: () => { activeWizardOverlay = null; }, footer,
  });
  activeWizardOverlay = modal;
  const saveBtn = modal.querySelector('.modal-foot .btn-primary');
  saveBtn.disabled = true;
  const updateSaveBtn = () => {
    saveBtn.textContent = pickSaveLabel();
    saveBtn.disabled = picker.picked.size === 0;
  };
  loadCategories().then((rows) => {
    picker.rows = rows;
    modal.querySelector('.modal-body').innerHTML = categoryPickerBodyHtml();
    wirePickerToolbar(updateSaveBtn);
    wirePickerViewToggle();
    wirePickerAccountFilter();
    rerenderPickerTable();
    updateSaveBtn();
  }).catch((err) => {
    modal.querySelector('.modal-body').innerHTML = `<span class="preview-hint">${escHtml(err.message || 'Could not load categories.')}</span>`;
  });
}

// ── Boot ──────────────────────────────────────────────────────────────────────
document.getElementById('hr-add-btn').addEventListener('click', () => openAddRuleWizard());

function renderAll() {
  renderScopeControl();
  renderRulesSection();
}
renderAll();
