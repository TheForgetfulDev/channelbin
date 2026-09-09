/* The filename template designer (DESIGN.md 15.1 / 15.4).

   Replaces /settings/template and templates/template_editor.html, both deleted outright in
   the same change (DESIGN.md 11.4: a losing spelling gets deleted, not switched off).

   It is written as a COMPONENT rather than a page script. `openFilenameDesigner()` is the
   modal host Settings uses; `designerBodyHtml()` and the delegated wiring below are the
   seam a second host swaps into its own panel - a Recording Profile's template field is
   the next one, and the fixed ordering (2026-07-27) is that the Settings designer is
   revamped first so that field adopts this rather than the old editor.

   THREE THINGS THIS FILE DELIBERATELY DOES NOT DO:

   1. It does not spell _safe_name() in JavaScript. The preview is computed server-side by
      /api/template-preview, which returns app/recorder.py::_safe_name's own output. The
      one thing this screen exists to show is the name that lands in /dvr, so a second
      implementation of that rule is the defect it is trying to prevent - and a JS copy
      would have to re-derive Python's unicode-aware \w by hand.
   2. It does not build a dropdown. `Start from an example` and the two tag-cleanup pickers
      are three registry entries over static/js/dropdown.js (DESIGN.md 15.3).
   3. It does not open a second modal for the guide picker. The picker is a STEP inside the
      one overlay: two stacked overlays would mean two Escape handlers arguing about which
      one closes, and the scroll lock is derived from the DOM. Designer state lives in `S`,
      so coming back rebuilds it exactly.

   Rollout: dev/changelog/441. */
(() => {
  'use strict';

/* Round 3 moved these off the page and into the Template group's own dropdown. As a
   labeled block of four full-width buttons they sat between the template box and the
   controls that act on it, which made it hard to tell what belonged to
   what. Rendered as the ON-DISK spelling, matching the preview line - an example menu that
   renders a template one way while the preview renders it another is the exact mismatch
   round 4 removed from the preview block. */
const FD_EXAMPLES = [
  '{date} - {title} - {channel}',
  '{channel} - {date} {start_time} - {title}',
  '{date} - {title} ({channel})',
  '{date} - {title} {tag:live}',
];

const FD_DEBOUNCE_MS = 250;
const FD_PICKER_PAGE = 200;

let FD_BOOT = null;          // one /api/filename-designer fetch per page load
let fdModal = null;
let fdOnSave = null;
let fdPreviewTimer = null;
let fdPickerTimer = null;
/* Debounced requests can land out of order, and a stale response overwriting a newer one
   would show a filename for a template that is no longer in the box. Every fetch takes a
   ticket and only the newest one is allowed to write. Same shape as the "timestamp anchors
   only move forward" rule in CLAUDE.md, applied to responses. */
let fdSeq = 0;
let fdPickSeq = 0;

const S = {
  step: 'designer',
  tpl: '',
  remove: [],
  replace: [],
  src: 'sample',
  epgId: null,
  epgRow: null,               // the picked airing, for the subject line
  custom: { title: 'Premier League: Arsenal v Man City', sub_title: '', channel: 'NBC Sports HD',
            category: 'Sports', date: '', start: '', end: '', description: '' },
  preview: null,              // the last /api/template-preview response
  pkq: '',
  pkday: 'all',
  pkRows: [],
  pkTotal: 0,
  pkLoading: false,
};

/* ── Formatting. All of it is util.js's, which reads the display timezone and clock format
      from the page-wide <meta> tags base.html renders. The designer used to carry its own
      copies fed from its API payload, to avoid depending on a config global named after one
      host page - a page-neutral source satisfies that constraint outright, so the payload no
      longer carries the two values. ────────────────────────────────────────────────────── */
const fdInstant = utcIsoToDate;

function fdTimeText(iso) {
  const d = fdInstant(iso);
  return d ? fmtTimeTz(d) : '';
}

function fdDayLabel(iso) {
  const d = fdInstant(iso);
  return d ? tzDayLabel(d) : '';
}

function fdStartsIn(row) {
  if (row.on_now) return 'on now';
  if (row.ended) return 'ended';
  const d = fdInstant(row.start_time);
  if (!d) return '';
  const mins = Math.round((d.getTime() - Date.now()) / 60000);
  if (mins < 0) return '';
  if (mins < 60) return `in ${mins} min`;
  if (mins < 24 * 60) {
    const h = Math.floor(mins / 60), m = mins % 60;
    return `in ${h}h${m ? ' ' + m + 'm' : ''}`;
  }
  return '';
}

/* ── The preview request ─────────────────────────────────────────────────────────────
   Everything that changes the filename travels in one request, so the filename, the
   substitution note, the unknown-variable warning, the subject line and the example
   menu's four renderings all come out of ONE computation (DESIGN.md 15.7). Two of them
   computed separately is two things that can disagree about what is being previewed. */
function fdPreviewParams() {
  const p = new URLSearchParams();
  p.set('template', S.tpl);
  S.remove.forEach((n) => p.append('remove', n));
  S.replace.forEach((n) => p.append('replace', n));
  p.set('src', S.src === 'guide' ? 'epg' : S.src);
  if (S.src === 'guide' && S.epgId) p.set('epg_id', String(S.epgId));
  if (S.src === 'custom') {
    Object.entries(S.custom).forEach(([k, v]) => p.set(k, v || ''));
  }
  FD_EXAMPLES.forEach((t) => p.append('also', t));
  return p;
}

function fdFetchPreview() {
  const seq = ++fdSeq;
  if (!S.tpl.trim()) {
    // An empty template is a state the box can be in, and the API rejects it. Say so where
    // the filename goes rather than leaving the last valid name sitting there as if it
    // still described the box.
    S.preview = { disk: '', name: '', changed: false, unknown: [], also: [],
                  empty: true, subject: S.preview ? S.preview.subject : null };
    refreshPreviewOnly();
    return Promise.resolve();
  }
  return fetch(`/api/template-preview?${fdPreviewParams().toString()}`)
    .then((r) => r.json())
    .then((d) => {
      if (seq !== fdSeq || !d.success) return;
      S.preview = d;
      refreshPreviewOnly();
      // The subject line names the program the filename above it describes, so it is
      // rewritten from the same response - never from what the page thought was picked.
      const src = document.getElementById('fd-src');
      if (src) src.innerHTML = subjectHtml();
    })
    .catch(() => { /* a dropped preview leaves the last good one on screen */ });
}

function schedulePreview() {
  clearTimeout(fdPreviewTimer);
  fdPreviewTimer = setTimeout(fdFetchPreview, FD_DEBOUNCE_MS);
}

/* ── Markup ──────────────────────────────────────────────────────────────────────── */

/* Written only when _safe_name actually changed something, so a template that already
   survives intact carries no sentence explaining a substitution that did not happen
   (DESIGN.md 15.4 - principle 1 cuts both ways, and an unexplained note is its own noise). */
function safeNoteHtml() {
  const p = S.preview;
  if (!p || !p.changed) return '';
  return 'Spaces and punctuation outside <code>A-Z a-z 0-9 _ - .</code> become '
    + '<span class="sub">_</span> when the file is written.';
}

function warnText() {
  const unknown = (S.preview && S.preview.unknown) || [];
  if (!unknown.length) return '';
  const verb = unknown.length === 1 ? 'is not a variable' : 'are not variables';
  return `⚠ ${unknown.join(', ')} ${verb} - it will appear literally in the filename.`;
}

/* The subject line: three sources, and each says what it IS rather than just showing a
   title. Read off the preview response, so it cannot describe a different program than
   the filename above it. */
function subjectHtml() {
  const subj = (S.preview && S.preview.subject) || null;
  const seg = `<div class="fd-srcseg">
      <button type="button" data-fdsrc="sample"${S.src === 'sample' ? ' class="on"' : ''}>Sample</button>
      <button type="button" data-fdsrc="guide"${S.src === 'guide' ? ' class="on"' : ''}>From the guide</button>
      <button type="button" data-fdsrc="custom"${S.src === 'custom' ? ' class="on"' : ''}>Custom</button>
    </div>`;
  let body;
  if (S.src === 'guide') {
    const row = S.epgRow;
    body = row
      ? `<div class="fd-subj"><span class="sname">${escHtml(row.title)}</span>
           <span class="text-faint">${escHtml(row.channel.name)} &middot; ${escHtml(fdDayLabel(row.start_time))} ${escHtml(fdTimeText(row.start_time))}</span>
           <button class="btn btn-sm" type="button" data-fdpick-open>Change</button></div>`
      : `<div class="fd-subj"><span class="samp-badge">sample</span>
           <span class="sname">${escHtml(subj ? subj.title : '')}</span>
           <button class="btn btn-sm" type="button" data-fdpick-open>Pick from guide</button></div>`;
  } else if (S.src === 'custom') {
    body = `<div class="fd-subj"><span class="sname">${escHtml(S.custom.title || '(untitled)')}</span>
      <span class="text-faint">${escHtml(S.custom.channel || 'no channel')}</span></div>`;
  } else {
    body = `<div class="fd-subj"><span class="samp-badge">sample</span>
      <span class="sname">${escHtml(subj ? subj.title : '')}</span>
      <span class="text-faint">on ${escHtml(subj ? subj.channel : '')}</span></div>`;
  }
  // A picked airing that has since been swept out of the EPG degrades LOUDLY: the server
  // fell back to the sample, and saying nothing would silently rename what is on screen.
  const fell = subj && subj.fell_back
    ? `<div class="fd-note" style="flex-basis:100%">That program is no longer in the guide, so this is the
         app's built-in sample. Pick another showing to preview a real one.</div>`
    : '';
  return seg + body + fell;
}

/* What each enabled rule does, said in words rather than reverse-engineered from the
   preview string. The `Applied to this filename` caption lives INSIDE this renderer, so
   the empty state cannot carry a caption over an empty list (DESIGN.md 15.4). */
function ruleListHtml() {
  const rules = cleanupRules();
  if (!rules.length) {
    return '<div class="rule-none">No cleanup rules yet. Pick a tag above to see what it would do.</div>';
  }
  const items = rules.map(({ tag, mode }) => {
    const pats = tag.patterns.map((p) => `<code>${escHtml(p)}</code>`).join(', ');
    const verb = mode === 'remove'
      ? `${pats} ${tag.patterns.length === 1 ? 'is' : 'are'} deleted from the filename`
      : `${pats} in the filename become <code>${escHtml(tag.name)}</code>`;
    return `<div class="rule"><span class="rn"><span class="tagdot" style="background:${escHtml(tag.color)}"></span> ${escHtml(tag.name)}</span>
      <span class="rtxt">${verb}</span></div>`;
  }).join('');
  return `<div class="rule-applied"><div class="rule-lbl">Applied to this filename</div>
    <div class="rulelist">${items}</div></div>`;
}

function tagByName(name) {
  return (FD_BOOT.tags || []).find((t) => t.name === name) || null;
}

function cleanupRules() {
  return [
    ...S.remove.map((n) => ({ tag: tagByName(n), mode: 'remove' })),
    ...S.replace.map((n) => ({ tag: tagByName(n), mode: 'replace' })),
  ].filter((r) => r.tag);
}

function designerBodyHtml() {
  const p = S.preview || {};
  const vars = (FD_BOOT.variables || []);
  const tags = (FD_BOOT.tags || []);
  return `<div class="fd">

    <div class="fd-sticky">
      <div class="fd-result">
        <div class="rl">Resulting filename</div>
        <div class="fd-name" id="fd-name">${escHtml(p.disk || '')}<span class="ext">.${escHtml(FD_BOOT.extension || 'mp4')}</span></div>
        <div class="fd-safe" id="fd-safe">${safeNoteHtml()}</div>
      </div>
      <div class="fd-src" id="fd-src">${subjectHtml()}</div>
    </div>

    ${S.src === 'custom' ? `<section class="fd-group">
      <div class="fd-group-head">
        <label class="fd-lbl">Preview program</label>
        <span class="text-faint" style="font-size:.74rem">Nothing here is saved. It only feeds the preview above.</span>
      </div>
      <div class="cust-grid">
        <label class="wide">Title<input type="text" data-fdcust="title" value="${escHtml(S.custom.title)}"></label>
        <label class="wide">Sub-title<input type="text" data-fdcust="sub_title" value="${escHtml(S.custom.sub_title)}"></label>
        <label>Channel<input type="text" data-fdcust="channel" value="${escHtml(S.custom.channel)}"></label>
        <label>Category<input type="text" data-fdcust="category" value="${escHtml(S.custom.category)}"></label>
        <label>Date<input type="date" data-fdcust="date" value="${escHtml(S.custom.date)}"></label>
        <label>Start<input type="time" data-fdcust="start" value="${escHtml(S.custom.start)}"></label>
        <label>End<input type="time" data-fdcust="end" value="${escHtml(S.custom.end)}"></label>
        <label class="wide">Description<input type="text" data-fdcust="description" value="${escHtml(S.custom.description)}"></label>
      </div>
    </section>` : ''}

    <section class="fd-group">
      <div class="fd-group-head">
        <label class="fd-lbl" for="fd-tpl">Template</label>
        <span id="fd-ex">${dropdownTriggerHtml('fdex')}</span>
      </div>
      <input type="text" class="fd-tpl" id="fd-tpl" value="${escHtml(S.tpl)}" spellcheck="false"
             aria-label="Filename template">
      <div class="fd-warn${warnText() ? ' on' : ''}" id="fd-warn">${escHtml(warnText())}</div>
      <label class="fd-lbl" style="margin-top:12px">Variables
        <span class="text-faint" style="font-weight:400">(click to insert at the cursor)</span></label>
      <div class="vchips">
        ${vars.map((v) => `<button class="vchip" type="button" data-fdvar="${escHtml(v.name)}" data-tip="${escHtml(v.desc)}">${escHtml(v.name)}</button>`).join('')}
        ${tags.length ? `<span class="tagcombo" data-tip="A conditional insert. It renders as the tag's NAME when one of that tag's patterns appears in the program's title, sub-title or description, and as nothing at all when none does.">
          <span>{tag:</span>
          <select id="fd-tagpick" aria-label="Tag to insert">${tags.map((t) => `<option value="${escHtml(t.name)}">${escHtml(t.name)}</option>`).join('')}</select>
          <span>}</span>
          <button type="button" class="tc-add" id="fd-tagadd">Insert</button>
        </span>` : ''}
      </div>
      <div class="fd-hint">Folders belong to a Recording Profile, not to the filename template -
        a <code>/</code> typed here is flattened to <code>_</code> when the file is written.</div>
    </section>

    <section class="fd-group">
      <div class="fd-group-head"><label class="fd-lbl">Tag cleanup</label></div>
      <div class="fl-desc" style="margin-bottom:9px">Applied to the fully-rendered filename, wherever the
        tag's pattern appears (title, sub-title, description). A tag can only be in one list at a time.</div>
      ${tags.length
        ? `<div class="tagpicks">${dropdownTriggerHtml('fdtag:remove')}${dropdownTriggerHtml('fdtag:replace')}</div>
           <div id="fd-rules">${ruleListHtml()}</div>`
        : '<div class="rule-none">No tags yet, so there is nothing to clean up. Tags are managed on the Tags page.</div>'}
    </section>

  </div>`;
}

/* Only the live regions are rewritten on a keystroke, never the whole designer - retyping
   it would move the caret out of the template box on every character. */
function refreshPreviewOnly() {
  const nameEl = document.getElementById('fd-name');
  if (!nameEl) return;
  const p = S.preview || {};
  nameEl.innerHTML = escHtml(p.disk || '')
    + `<span class="ext">.${escHtml(FD_BOOT.extension || 'mp4')}</span>`;
  document.getElementById('fd-safe').innerHTML = safeNoteHtml();
  const warn = document.getElementById('fd-warn');
  const msg = p.empty ? 'A template cannot be empty.' : warnText();
  warn.textContent = msg;
  warn.classList.toggle('on', !!msg);
  const rules = document.getElementById('fd-rules');
  if (rules) rules.innerHTML = ruleListHtml();
  // The trigger labels re-derive from state, so a tag ticked in one picker shows up as
  // untickable in the other without either of them being patched at the click site.
  syncDropdownTriggers();
  const save = document.querySelector('[data-fd-save]');
  if (save) save.disabled = !S.tpl.trim();
}

/* A whole-designer redraw, for the changes that move more than the preview (the source
   segment, the custom-program fields appearing). It is the ONE writer for that region, so
   the subject line and the filename above it always describe the same program. */
function redrawDesigner() {
  if (!fdModal || S.step !== 'designer') return;
  fdModal.querySelector('.modal-body').innerHTML = designerBodyHtml();
  syncDropdownTriggers();
}

/* ── The guide picker, a STEP inside the same overlay ────────────────────────────────
   The rows come from the app's one airing search (app/channel_search.py via
   /api/channels/search?grain=airings), never a second query built here: CLAUDE.md makes
   that engine the single home for "when is this on", and its default ordering is already
   every future showing, earliest first. What is NOT lifted from that page is the rest of
   it - no facet rail, no selection bar, no columns picker. This returns one airing. */
function fdPickerFetch() {
  const seq = ++fdPickSeq;
  S.pkLoading = true;
  // `per_page`, which is the engine's own spelling (app/channel_search.py) - `page_size` is
  // what it sends BACK, and sending that name is silently ignored and served the default.
  const p = new URLSearchParams({ grain: 'airings', per_page: String(FD_PICKER_PAGE) });
  if (S.pkq.trim()) p.set('q', S.pkq.trim());
  return fetch(`/api/channels/search?${p.toString()}`)
    .then((r) => r.json())
    .then((d) => {
      if (seq !== fdPickSeq) return;
      S.pkLoading = false;
      S.pkRows = (d.success && d.rows) ? d.rows : [];
      /* `null`, NOT 0, when the endpoint declined to count. `|| 0` collapsed the two, so a
         search running without its index rendered "no showings" over a list of showings
         (dev/changelog/676). Rows in hand are the honest answer when the total is absent. */
      S.pkTotal = (d.success && typeof d.total === 'number') ? d.total : null;
      refreshPicker();
    })
    .catch(() => {
      if (seq !== fdPickSeq) return;
      S.pkLoading = false;
      S.pkRows = [];
      S.pkTotal = 0;
      refreshPicker();
    });
}

function pickerDays() {
  const seen = [];
  S.pkRows.forEach((r) => {
    const d = fdInstant(r.start_time);
    if (!d) return;
    const k = tzDayKey(d);
    if (!seen.includes(k)) seen.push(k);
  });
  return seen;
}

function pickerMatches() {
  if (S.pkday === 'all') return S.pkRows;
  return S.pkRows.filter((r) => {
    const d = fdInstant(r.start_time);
    return d && tzDayKey(d) === S.pkday;
  });
}

function pickerRowsHtml(rows) {
  if (S.pkLoading) return '<div class="pk-empty">Searching what is on&hellip;</div>';
  if (!rows.length) {
    return `<div class="pk-empty">${S.pkq.trim()
      ? `Nothing on matches <strong>${escHtml(S.pkq.trim())}</strong>.`
      : 'Your TV Guide has no upcoming programs.<br>Add channels to the TV Guide and they will show up here.'}</div>`;
  }
  return rows.map((r) => {
    const initials = (r.channel.name || '').replace(/[^A-Za-z]/g, '').slice(0, 2).toUpperCase() || '--';
    const rel = fdStartsIn(r);
    return `<button class="pk-row${r.id === S.epgId ? ' on' : ''}" type="button" data-fdpick="${escHtml(String(r.id))}">
      <span class="pk-logo">${escHtml(initials)}</span>
      <span class="pk-cell">
        <span class="pk-title">${escHtml(r.title)}</span>
        ${r.sub_title ? `<span class="pk-sub">${escHtml(r.sub_title)}</span>` : ''}
      </span>
      <span class="pk-meta">
        <span class="pk-chan">${escHtml(r.channel.name)}</span>
        <span class="pk-when${r.on_now ? ' on-now' : ''}">
          <span class="pw-day">${escHtml(fdDayLabel(r.start_time))}</span>
          <span class="pw-time">${escHtml(fdTimeText(r.start_time))} to ${escHtml(fdTimeText(r.stop_time))}</span>
          ${rel ? `<span class="pw-rel${r.on_now ? ' now' : ''}">${escHtml(rel)}</span>` : ''}
        </span>
      </span>
    </button>`;
  }).join('');
}

function pickerBodyHtml() {
  const rows = pickerMatches();
  return `<div class="pk-head">
      <div class="search-wrap${S.pkq ? ' has-text' : ''}" id="fd-pkwrap">
        <input type="text" class="search" id="fd-pksearch" autocomplete="off" value="${escHtml(S.pkq)}"
               placeholder="Search what is on by program or channel">
        <button class="search-clear" id="fd-pkclear" type="button" aria-label="Clear the search">&times;</button>
      </div>
      <span class="pk-count" id="fd-pkcount">${pickerCountText(rows)}</span>
    </div>
    <div class="pk-days" id="fd-pkdays">${pickerDaysHtml()}</div>
    <div class="pk-list" id="fd-pklist">${pickerRowsHtml(rows)}</div>`;
}

/* The page against the true total, not the page against itself: this list is capped at
   FD_PICKER_PAGE showings, and a count that hid that would claim the guide holds fewer
   programs than it does. */
function pickerCountText(rows) {
  if (S.pkLoading) return 'searching…';
  const shown = rows.length;
  // An uncounted total is not a zero one: say what is actually in hand rather than
  // claiming there are no showings while showing some.
  if (S.pkTotal === null) return shown ? `${shown} showings` : 'no showings';
  if (!S.pkTotal) return 'no showings';
  return shown === S.pkTotal ? `${shown} showings` : `${shown} of ${S.pkTotal} showings`;
}

function pickerDaysHtml() {
  const days = pickerDays();
  return `<button class="chip${S.pkday === 'all' ? ' active' : ''}" type="button" data-fdpkday="all">All days</button>`
    + days.map((k) => {
      // Noon on that day, so the label cannot land on the previous one through a DST shift.
      const label = fdDayLabel(`${k}T12:00:00`);
      return `<button class="chip${S.pkday === k ? ' active' : ''}" type="button" data-fdpkday="${escHtml(k)}">${escHtml(label)}</button>`;
    }).join('');
}

/* Only the list, the count and the chip states are rewritten while the picker filters -
   never the search input itself, or every keystroke would destroy the node the caret is
   sitting in. */
function refreshPicker() {
  if (!fdModal || S.step !== 'picker') return;
  const rows = pickerMatches();
  fdModal.querySelector('#fd-pklist').innerHTML = pickerRowsHtml(rows);
  fdModal.querySelector('#fd-pkcount').textContent = pickerCountText(rows);
  fdModal.querySelector('#fd-pkdays').innerHTML = pickerDaysHtml();
  fdModal.querySelector('#fd-pkwrap').classList.toggle('has-text', S.pkq.length > 0);
}

/* ── Step rendering. One overlay, two bodies and two feet. ──────────────────────────── */
const FD_DESIGNER_FOOT = `<span class="modal-foot-note">Saved as recording.filename_template. The same
    designer is what a Recording Profile's own template field will open.</span>
  <button class="btn" type="button" data-fd-cancel>Cancel</button>
  <button class="btn btn-primary" type="button" data-fd-save>Save</button>`;

const FD_PICKER_FOOT = `<span class="modal-foot-note">The full search - facets, saved searches, the
    columns picker - lives on the Channels page. This is the same rows, cut down to picking one.</span>
  <button class="btn" type="button" data-fd-back>&larr; Template</button>`;

function renderStep() {
  if (!fdModal) return;
  const designer = S.step === 'designer';
  fdModal.querySelector('.modal-head h2').textContent = designer ? 'Filename template' : 'Pick a program';
  fdModal.querySelector('.modal-body').innerHTML = designer ? designerBodyHtml() : pickerBodyHtml();
  fdModal.querySelector('.modal-foot').innerHTML = designer ? FD_DESIGNER_FOOT : FD_PICKER_FOOT;
  if (designer) {
    syncDropdownTriggers();
    const save = fdModal.querySelector('[data-fd-save]');
    if (save) save.disabled = !S.tpl.trim();
  }
}

function openPickerStep() {
  closeDropdown();
  S.step = 'picker';
  renderStep();
  fdPickerFetch();
  const box = fdModal.querySelector('#fd-pksearch');
  if (box) box.focus();
}

function backToDesigner() {
  closeDropdown();
  S.step = 'designer';
  renderStep();
}

/* One insertion behavior for the variable chips and for the {tag:...} combo, rather than
   two that can drift. */
function insertAtCursor(text) {
  const box = fdModal && fdModal.querySelector('#fd-tpl');
  if (!box) return;
  const pos = box.selectionStart == null ? box.value.length : box.selectionStart;
  const end = box.selectionEnd == null ? pos : box.selectionEnd;
  box.value = box.value.slice(0, pos) + text + box.value.slice(end);
  S.tpl = box.value;
  box.focus();
  box.selectionStart = box.selectionEnd = pos + text.length;
  refreshPreviewOnly();
  schedulePreview();
}

/* ── The three dropdowns: registry entries, not implementations (DESIGN.md 15.3) ────── */
function registerDesignerDropdowns() {
  registerDropdown('fdex', {
    single: true,
    title: () => 'Start from an example',
    // The on-disk spelling of each example, rendered by the SAME request that rendered the
    // live preview - so the menu cannot show one substitution rule and the preview another.
    rows: () => {
      const also = (S.preview && S.preview.also) || [];
      const byTpl = {};
      also.forEach((a) => { byTpl[a.template] = a.disk; });
      return FD_EXAMPLES.map((t) => ({ v: t, label: t, mono: true, sub: byTpl[t] || '' }));
    },
    label: () => 'Start from an example',
    pick: (_arg, value) => {
      S.tpl = value;
      const box = fdModal && fdModal.querySelector('#fd-tpl');
      if (box) box.value = value;
      fdFetchPreview();
    },
  });

  registerDropdown('fdtag', {
    title: (mode) => (mode === 'remove' ? 'Remove from filename' : 'Replace with the tag name'),
    rows: () => (FD_BOOT.tags || []).map((t) => ({
      v: t.name, label: t.name, dot: t.color, right: t.patterns.join(', '),
    })),
    on: (mode) => (mode === 'remove' ? S.remove : S.replace),
    label: (mode) => {
      const picked = mode === 'remove' ? S.remove : S.replace;
      const noun = mode === 'remove' ? 'Remove: ' : 'Replace: ';
      return noun + ddPickLabel(picked, 'tags');
    },
    // Mutual exclusion enforced HERE rather than at the click site: a name can only be in
    // one list, and the server drops a name from `replace` if it is also in `remove`, so
    // letting the two controls disagree would show a state that cannot be saved.
    toggle: (mode, value, checked) => {
      const mine = mode === 'remove' ? S.remove : S.replace;
      const other = mode === 'remove' ? S.replace : S.remove;
      const drop = (list, v) => { const i = list.indexOf(v); if (i !== -1) list.splice(i, 1); };
      drop(mine, value);
      drop(other, value);
      if (checked) mine.push(value);
      refreshPreviewOnly();
      fdFetchPreview();
    },
  });
}

/* ── Wiring. Delegated on the overlay, so a region that gets rebuilt (the designer, the
      picker list, the day chips) keeps working without re-binding. ──────────────────── */
function wireDesigner(overlay) {
  overlay.addEventListener('click', (e) => {
    const t = e.target;

    if (t.closest('[data-fd-cancel]')) { overlay.closeModal(); return; }
    if (t.closest('[data-fd-save]')) { saveTemplate(); return; }
    if (t.closest('[data-fdpick-open]')) { openPickerStep(); return; }
    if (t.closest('[data-fd-back]')) { backToDesigner(); return; }

    const src = t.closest('[data-fdsrc]');
    if (src) {
      S.src = src.dataset.fdsrc;
      // Entering `From the guide` with nothing picked goes straight to the picker: the
      // segment would otherwise land on a state whose only content is a button.
      if (S.src === 'guide' && !S.epgId) { openPickerStep(); return; }
      redrawDesigner();
      fdFetchPreview();
      return;
    }

    const vchip = t.closest('[data-fdvar]');
    if (vchip) { insertAtCursor(vchip.dataset.fdvar); return; }
    if (t.closest('#fd-tagadd')) {
      const sel = overlay.querySelector('#fd-tagpick');
      if (sel && sel.value) insertAtCursor(`{tag:${sel.value}}`);
      return;
    }

    const pick = t.closest('[data-fdpick]');
    if (pick) {
      const id = Number(pick.dataset.fdpick);
      S.epgId = id;
      S.epgRow = S.pkRows.find((r) => r.id === id) || null;
      S.src = 'guide';
      backToDesigner();
      fdFetchPreview();
      return;
    }
    const day = t.closest('[data-fdpkday]');
    if (day) { S.pkday = day.dataset.fdpkday; refreshPicker(); return; }
    if (t.closest('#fd-pkclear')) {
      S.pkq = '';
      const box = overlay.querySelector('#fd-pksearch');
      if (box) { box.value = ''; box.focus(); }
      fdPickerFetch();
    }
  });

  overlay.addEventListener('input', (e) => {
    const t = e.target;
    if (t.id === 'fd-tpl') { S.tpl = t.value; schedulePreview(); return; }
    if (t.id === 'fd-pksearch') {
      S.pkq = t.value;
      // The day chips are derived from the returned page, so a new query has to re-fetch
      // rather than filter what is already here - the previous page is a different set.
      S.pkday = 'all';
      clearTimeout(fdPickerTimer);
      fdPickerTimer = setTimeout(fdPickerFetch, FD_DEBOUNCE_MS);
      return;
    }
    const cust = t.closest('[data-fdcust]');
    if (cust) { S.custom[cust.dataset.fdcust] = t.value; schedulePreview(); }
  });
}

function saveTemplate() {
  if (!S.tpl.trim()) return;
  jsonFetch('/api/filename-template', {
    method: 'POST',
    body: JSON.stringify({ template: S.tpl, remove: S.remove, replace: S.replace }),
  }).then((d) => {
    if (!d) return;
    showToast('Filename template saved.');
    // The boot payload is the component's memory of what is stored, so reopening the
    // designer without a page reload has to see what was just saved rather than what was
    // there when the page loaded.
    if (FD_BOOT) { FD_BOOT.template = d.template; FD_BOOT.remove = d.remove; FD_BOOT.replace = d.replace; }
    if (fdOnSave) fdOnSave(d);
    if (fdModal) fdModal.closeModal();
  }).catch((e) => showToast(e.message || 'Could not save the template.', { type: 'error' }));
}

/* ── The modal host ──────────────────────────────────────────────────────────────── */
function fdLoadBoot() {
  if (FD_BOOT) return Promise.resolve(FD_BOOT);
  return fetch('/api/filename-designer').then((r) => r.json()).then((d) => {
    if (!d.success) throw new Error(d.error || 'boot failed');
    FD_BOOT = d;
    registerDesignerDropdowns();
    return d;
  });
}

/* `onSave` is how a host learns the template changed - Settings uses it to update the
   value it shows on the field row. The component never reaches into its host's DOM. */
function openFilenameDesigner({ onSave = null } = {}) {
  fdOnSave = onSave;
  fdLoadBoot().then(() => {
    S.step = 'designer';
    S.tpl = FD_BOOT.template || '';
    S.remove = (FD_BOOT.remove || []).slice();
    S.replace = (FD_BOOT.replace || []).slice();
    S.preview = null;
    // The first preview is fetched BEFORE the panel is built, so the modal never opens
    // showing an empty filename that fills in a moment later - the filename is the whole
    // point of the screen and a blank first frame reads as a broken one.
    return fdFetchPreview().then(() => {
      fdModal = buildModal({
        title: 'Filename template',
        body: designerBodyHtml(),
        panelClass: 'modal-xwide fd-modal',
        // Both debounce timers are cancelled on close: a pending fetch that lands after
        // the overlay is gone would write into a DOM that no longer exists.
        onClose: () => {
          closeDropdown();
          fdModal = null;
          clearTimeout(fdPreviewTimer);
          clearTimeout(fdPickerTimer);
        },
      });
      // buildModal only builds a foot when it is given footer entries, and this component
      // owns both steps' feet as markup so the delegated wiring covers them the same way
      // it covers everything else.
      const foot = document.createElement('div');
      foot.className = 'modal-foot';
      foot.innerHTML = FD_DESIGNER_FOOT;
      fdModal.querySelector('.modal-panel').appendChild(foot);
      wireDesigner(fdModal);
      syncDropdownTriggers();
      const save = fdModal.querySelector('[data-fd-save]');
      if (save) save.disabled = !S.tpl.trim();
      const box = fdModal.querySelector('#fd-tpl');
      if (box) box.focus();
    });
  }).catch(() => showToast('Could not open the filename designer.', { type: 'error' }));
}

  // The modal host is what Settings calls today. `FilenameDesigner` is the seam a second
  // host uses to put the same body into its own panel - a Recording Profile's template
  // field is the next one, and exposing the pieces rather than only the modal is what
  // keeps that from becoming a second implementation (DESIGN.md 15.1).
  window.openFilenameDesigner = openFilenameDesigner;
  window.FilenameDesigner = {
    open: openFilenameDesigner,
    bodyHtml: designerBodyHtml,
    wire: wireDesigner,
    loadBoot: fdLoadBoot,
    state: S,
  };
})();
