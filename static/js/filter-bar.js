/* The one list-page filter bar (DESIGN.md 3.11).

   "search input + '+ Filter' chip + Columns" is the approved list-page toolbar, and
   the middle control is this: one chip that opens a list of DIMENSIONS, each of which
   opens its own values, and every chosen value comes back as a removable chip. The bar
   therefore costs one control when nothing is filtered, and every filter that IS on is
   on screen as its own chip rather than hidden inside a control whose label has to
   summarize it.

   It exists because three pages had already written it three times - the recordings
   list inline in its template, the Groups tab, and the group detail page's four
   permanently-visible controls. A fourth surface is a registry entry, not a fourth
   implementation.

   A dimension is:
     { k:      'account',                     // stable key; what the URL carries
       label:  'Account',                     // chip prefix and menu row
       values: [{v, label}] | () => [...],    // a function when they come from the rows
       match:  (row, v) => bool,              // the ONE predicate for this dimension
       available: () => bool }                // optional; false hides it from the menu

   Values are strings - they make a round trip through a `data-` attribute, which has no
   other type. Values inside one dimension OR together and different dimensions AND,
   which is the only sensible reading of "Status: Fail, Warn" beside "Account: A".

   `row` is whatever the caller filters: a DOM node on a server-rendered list, a plain
   object on a JS-rendered one. Nothing here ever reads a row itself - `match` does - so
   the component does not care which.

   SAVED FILTERS are opt-in (opts.saved, below): a named chip combination, loadable, one
   of which may be the default applied as the page opens. Stored through the generic
   /api/user-prefs row so they follow the user across browsers (DESIGN.md 3.11).

   Rollout: dev/changelog/767; saved filters dev/changelog/1156. */

function createFilterBar(opts) {
  const chipsEl = opts.chipsEl;
  const menuEl = opts.menuEl;
  const dims = opts.dims || [];
  const rowsOf = opts.rows || (() => []);
  const onChange = opts.onChange || (() => {});

  // { dimKey: Set(value) }. Empty is the nothing-active state, and it is also what the
  // server renders, so JS only ever upgrades this page - it is never required to calm
  // one down. An "all values checked means no filter" reading would light the bar on
  // every page load, which is why nothing here has one.
  const active = {};
  // The drilled-into dimension; null is the dimension list. Held here rather than read
  // back off the popover's own markup, which is rewritten on every click.
  let openKey = null;

  const dimByKey = (k) => dims.find(d => d.k === k) || null;
  const valuesOf = (d) => (typeof d.values === 'function' ? (d.values() || []) : (d.values || []));
  const isOffered = (d) => (d.available ? !!d.available() : true);
  const labelOf = (d, v) => {
    const o = valuesOf(d).find(x => x.v === v);
    return o ? o.label : v;
  };

  const selected = (k) => active[k] || new Set();
  const has = (k, v) => !!active[k] && active[k].has(v);
  const count = () => Object.keys(active).reduce((n, k) => n + active[k].size, 0);

  function toggle(k, v) {
    if (!dimByKey(k)) return;
    if (!active[k]) active[k] = new Set();
    if (active[k].has(v)) active[k].delete(v); else active[k].add(v);
    if (!active[k].size) delete active[k];
  }

  function clear() {
    Object.keys(active).forEach(k => delete active[k]);
  }

  // Flat [key, value] pairs, the same shape setFrom() takes, so a caller that persists
  // the bar to the URL reads and writes one format.
  function entries() {
    const out = [];
    dims.forEach(d => { if (active[d.k]) active[d.k].forEach(v => out.push([d.k, v])); });
    return out;
  }

  function setFrom(pairs) {
    (pairs || []).forEach(pair => {
      const k = pair[0], v = String(pair[1]);
      if (!dimByKey(k)) return;
      if (!active[k]) active[k] = new Set();
      active[k].add(v);
    });
  }

  function matches(row) {
    return Object.keys(active).every(k => {
      const d = dimByKey(k);
      if (!d) return true;
      return Array.from(active[k]).some(v => d.match(row, v));
    });
  }

  // A filter the user cannot SEE is the one state this must never come to rest in. Rows
  // move under a live list - an account's last member leaves, a value stops occurring -
  // so a selection whose dimension is no longer offered, or whose value no longer
  // exists, is dropped rather than left hiding rows from behind a chip that is gone.
  function prune() {
    Object.keys(active).forEach(k => {
      const d = dimByKey(k);
      if (!d || !isOffered(d)) { delete active[k]; return; }
      const live = new Set(valuesOf(d).map(o => o.v));
      Array.from(active[k]).forEach(v => { if (!live.has(v)) active[k].delete(v); });
      if (!active[k].size) delete active[k];
    });
  }

  function renderChips() {
    chipsEl.querySelectorAll('.chip.active-filter').forEach(el => el.remove());
    const anchor = chipsEl.querySelector('.menu-wrap');
    // Chip order follows the registry, never the order they were clicked in: a bar that
    // reshuffles itself as you add a filter is one the eye has to re-read every time.
    dims.forEach(d => {
      if (!active[d.k]) return;
      Array.from(active[d.k]).forEach(v => {
        const text = `${d.label}: ${labelOf(d, v)}`;
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'chip active active-filter';
        chip.innerHTML = `${escHtml(text)} <span class="chip-x" aria-hidden="true">&#10005;</span>`;
        chip.setAttribute('aria-label', `Remove filter ${text}`);
        chip.addEventListener('click', () => { toggle(d.k, v); apply(); });
        chipsEl.insertBefore(chip, anchor);
      });
    });
  }

  function homeHtml() {
    const offered = dims.filter(isOffered);
    if (!offered.length) {
      return '<div class="pop-title">Filter</div>' +
        '<div class="pop-note">Nothing on this list can be filtered yet.</div>';
    }
    return '<div class="pop-title">Add a filter</div>' +
      offered.map(d => {
        const n = selected(d.k).size;
        return `<button class="menu-item" type="button" data-fdim="${escHtml(d.k)}">` +
          `${escHtml(d.label)}${n ? ` <span class="fb-n">${n}</span>` : ''}</button>`;
      }).join('') +
      (opts.note ? `<div class="pop-note">${escHtml(opts.note)}</div>` : '');
  }

  function dimHtml(d) {
    const vals = valuesOf(d);
    const back = '<button class="pop-back" type="button" data-fback>&#9666; All filters</button>' +
      `<div class="pop-title">${escHtml(d.label)}</div>`;
    if (!vals.length) return back + '<div class="pop-note">No values to filter by.</div>';
    // Counted against every row the caller owns, not against what the other filters have
    // already hidden: the number says how many rows carry this value, which is the
    // question being asked while choosing one.
    const rows = rowsOf() || [];
    return back + vals.map(o => {
      const on = has(d.k, o.v);
      const n = rows.filter(r => d.match(r, o.v)).length;
      return `<button class="menu-item fb-val${on ? ' on' : ''}" type="button" ` +
        `data-fdim="${escHtml(d.k)}" data-fval="${escHtml(o.v)}" aria-pressed="${on}">` +
        `<span class="fb-tick" aria-hidden="true">${on ? '&#10003;' : ''}</span>` +
        `<span class="fb-lbl">${escHtml(o.label)}</span>` +
        `<span class="fb-n">${n}</span></button>`;
    }).join('');
  }

  function renderMenu() {
    const d = openKey ? dimByKey(openKey) : null;
    if (d && isOffered(d)) menuEl.innerHTML = dimHtml(d);
    else { openKey = null; menuEl.innerHTML = homeHtml(); }
    // The popover changes height every time it is drilled into, so it is re-clamped
    // against the viewport instead of being left at the size it opened at. positionMenu
    // owns that rule app-wide (util.js); this only re-asks for it.
    if (typeof positionMenu === 'function' && menuEl._menuTrigger) {
      positionMenu(menuEl, menuEl._menuTrigger);
    }
  }

  // The chips, the popover and the caller's own list are redrawn from one state on every
  // change, so a filter can never be on in one of the three and off in another.
  function apply() {
    prune();
    renderChips();
    renderMenu();
    renderSavedChip();
    onChange();
  }

  function render() {
    prune();
    renderChips();
    renderMenu();
    renderSavedChip();
  }

  /* ── Saved filters (opts.saved) ─────────────────────────────────────
     opts.saved = { chipEl, menuEl, list, prefUrl }: the chip that opens the saved-filter
     popover, the popover itself, the list the server rendered into the page, and the
     /api/user-prefs URL the whole list is written back to. A record is
     { name, filters: [[k, v], ...], is_default } - `filters` is entries()'s own shape, so
     there is one spelling of a selection, not two.

     A saved filter holds the chips only: not the page's search text, not its sort. */
  const sv = opts.saved || null;
  let savedList = [];
  let loadedName = null;   // the one last loaded or saved; what "(edited)" is relative to
  let sfName = '';         // the name box, kept out of the DOM so a redraw cannot lose it
  // Set only when a write actually failed, so the popover says so for as long as it is
  // the truth rather than for a toast's five seconds. Cleared by the next good write.
  let sfError = '';

  // User-written JSON from the server: anything not shaped like a record is skipped
  // rather than allowed to throw on a page it only decorates. At most one default.
  function sanitize(list) {
    const out = [];
    let sawDefault = false;
    (Array.isArray(list) ? list : []).forEach(r => {
      if (!r || typeof r.name !== 'string' || !r.name.trim() || !Array.isArray(r.filters)) return;
      const filters = r.filters
        .filter(p => Array.isArray(p) && p.length === 2)
        .map(p => [String(p[0]), String(p[1])]);
      const isDefault = !!r.is_default && !sawDefault;
      if (isDefault) sawDefault = true;
      out.push({ name: r.name.trim(), filters, is_default: isDefault });
    });
    return out;
  }

  // What makes two selections "the same one": the same pairs, in any order.
  const pairsKey = (pairs) => JSON.stringify(
    (pairs || []).map(p => JSON.stringify([String(p[0]), String(p[1])])).sort());
  const savedByName = (name) => savedList.find(s => s.name === name) || null;

  /* DERIVED from the chips on screen, never a flag each handler must remember to set -
     one missed call site and a changed filter reads as unedited. */
  function savedStatus() {
    const key = pairsKey(entries());
    const loaded = loadedName && savedByName(loadedName);
    // The one just loaded wins over an identical twin saved under another name.
    if (loaded && pairsKey(loaded.filters) === key) return { name: loaded.name, dirty: false };
    if (count()) {
      const match = savedList.find(s => pairsKey(s.filters) === key);
      if (match) return { name: match.name, dirty: false };
    }
    return loaded ? { name: loaded.name, dirty: true } : { name: '', dirty: false };
  }

  function renderSavedChip() {
    if (!sv || !sv.chipEl) return;
    const st = savedStatus();
    sv.chipEl.innerHTML = st.name
      ? `Saved: <span class="fb-saved-cur">${escHtml(st.name)}</span>` +
        `${st.dirty ? ' <span class="fb-saved-dirty">(edited)</span>' : ''} &#9662;`
      : 'Saved &#9662;';
  }

  function savedHtml() {
    const rows = savedList.map((s, i) => `<div class="fb-saved-row">` +
      `<button class="menu-item fb-saved-load" type="button" data-fsload="${i}">` +
      `<span class="fb-saved-nm">${escHtml(s.name)}</span>` +
      `${s.is_default ? ' <span class="badge b-auto">default</span>' : ''}</button>` +
      `<button class="btn btn-sm" type="button" data-fsdef="${i}" data-tip="${s.is_default
        ? 'Stop applying this filter when the page opens.'
        : 'Apply this filter every time the page opens.'}">${s.is_default ? 'Clear default' : 'Set default'}</button>` +
      `<button class="btn btn-sm btn-icon" type="button" data-fsdel="${i}" ` +
      `aria-label="Delete saved filter ${escHtml(s.name)}">&times;</button></div>`).join('');
    return '<div class="pop-title">Saved filters</div>' +
      (rows || '<div class="fb-saved-empty">Nothing saved yet. Add filters, then name them here.</div>') +
      '<div class="fb-saved-foot">' +
      `<input class="fb-saved-input" type="text" data-fsname value="${escHtml(sfName)}" ` +
      'placeholder="Name these filters" autocomplete="off" aria-label="Saved filter name">' +
      '<button class="btn btn-sm btn-primary" type="button" data-fssave>Save</button></div>' +
      (sfError ? `<div class="fb-saved-err">${escHtml(sfError)}</div>` : '') +
      '<div class="pop-note">Saved to the server - follows you across browsers. A saved ' +
      'filter keeps the filter chips, not the search text.</div>';
  }

  function renderSavedMenu() {
    if (!sv || !sv.menuEl) return;
    sv.menuEl.innerHTML = savedHtml();
    if (typeof positionMenu === 'function' && sv.menuEl._menuTrigger &&
        sv.menuEl.classList.contains('open')) {
      positionMenu(sv.menuEl, sv.menuEl._menuTrigger);
    }
  }

  const toast = (msg, type) => { if (typeof showToast === 'function') showToast(msg, { type }); };

  /* Every change is painted first and written second, so a failed write has to undo the
     paint too - an in-memory list claiming what the server never stored is the one
     outcome this must not leave behind. */
  function persistSaved(rollback) {
    return jsonFetch(sv.prefUrl, { method: 'POST', body: JSON.stringify({ value: savedList }) })
      .then(() => { if (sfError) { sfError = ''; renderSavedMenu(); } })
      .catch(() => {
        rollback();
        sfError = 'Could not reach the server - that change was not saved, and has been undone here too.';
        renderSavedMenu();
        renderSavedChip();
        toast(sfError, 'error');
      });
  }

  const snapshotList = () => savedList.map(s => ({ ...s, filters: s.filters.map(p => p.slice()) }));

  /* Puts a saved selection on the bar. A value no row carries any more is dropped like
     any other (DESIGN.md 3.11), but NOT quietly: a saved "Channel: FS1" that loads as
     nothing would show every row under a name that promises a filter. */
  function loadPairs(s, notify) {
    clear();
    setFrom(s.filters);
    prune();
    const kept = new Set(entries().map(p => JSON.stringify(p)));
    const dropped = s.filters.filter(p => !kept.has(JSON.stringify(p)));
    loadedName = s.name;
    if (dropped.length) {
      const what = dropped.map(([k, v]) => {
        const d = dimByKey(k);
        return d ? `${d.label}: ${labelOf(d, v)}` : `${k}: ${v}`;
      }).join(', ');
      toast(`Nothing on this list matches ${what} right now, so "${s.name}" was applied without it.`, 'warning');
    }
    if (notify) apply(); else render();
  }

  function saveCurrent() {
    const name = sfName.trim();
    if (!name) { toast('Give the filter a name first.', 'error'); return; }
    if (!count()) { toast('Add a filter first - there is nothing to save yet.', 'error'); return; }
    const before = snapshotList();
    const prevLoaded = loadedName;
    const existing = savedByName(name);
    // Saving over an existing name is how "(edited)" is answered, so it overwrites - and
    // the toast says which of the two happened.
    if (existing) existing.filters = entries();
    else savedList.push({ name, filters: entries(), is_default: false });
    loadedName = name;
    sfName = name;
    persistSaved(() => { savedList = before; loadedName = prevLoaded; });
    renderSavedMenu();
    renderSavedChip();
    toast(existing ? `Updated "${name}".` : `Saved "${name}".`);
  }

  if (sv) {
    savedList = sanitize(sv.list);
    const def = savedList.find(s => s.is_default);
    // Applied before the first render, from data already in the page: the page script
    // runs before first paint, so the list never shows unfiltered and then jumps. The
    // caller is not notified (it is still being constructed) - it filters once after.
    if (def) loadPairs(def, false);

    if (sv.chipEl) {
      sv.chipEl.addEventListener('click', () => {
        sfName = loadedName || '';
        renderSavedMenu();
      });
    }
    if (sv.menuEl) {
      sv.menuEl.addEventListener('click', (e) => {
        const load = e.target.closest('[data-fsload]');
        if (load) {
          // A menu-item: util.js closes the popover after this, which is right for a load.
          const s = savedList[Number(load.dataset.fsload)];
          if (s) { sfName = s.name; loadPairs(s, true); }
          return;
        }
        const defBtn = e.target.closest('[data-fsdef]');
        if (defBtn) {
          e.stopPropagation();
          const i = Number(defBtn.dataset.fsdef);
          if (!savedList[i]) return;
          const before = snapshotList();
          const turnOn = !savedList[i].is_default;
          savedList.forEach((s, j) => { s.is_default = turnOn && j === i; });
          persistSaved(() => { savedList = before; });
          renderSavedMenu();
          toast(turnOn ? `"${savedList[i].name}" will be applied when the page opens.`
            : 'The page will open unfiltered.');
          return;
        }
        const del = e.target.closest('[data-fsdel]');
        if (del) {
          e.stopPropagation();
          const i = Number(del.dataset.fsdel);
          const gone = savedList[i];
          if (!gone) return;
          const before = snapshotList();
          const prevLoaded = loadedName;
          savedList = savedList.filter((_, j) => j !== i);
          if (loadedName === gone.name) loadedName = null;
          persistSaved(() => { savedList = before; loadedName = prevLoaded; });
          renderSavedMenu();
          renderSavedChip();
          toast(`Deleted "${gone.name}".`);
          return;
        }
        if (e.target.closest('[data-fssave]')) { e.stopPropagation(); saveCurrent(); }
      });
      sv.menuEl.addEventListener('input', (e) => {
        if (e.target.closest('[data-fsname]')) sfName = e.target.value;
      });
      sv.menuEl.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && e.target.closest('[data-fsname]')) {
          e.preventDefault();
          saveCurrent();
        }
      });
    }
  }

  menuEl.addEventListener('click', (e) => {
    // Every branch stops the event here. util.js closes an open menu when a
    // `button.menu-item` inside it is clicked, which is right for an action row and
    // wrong for a filter: picking Fail and then Warn is one visit to the popover, the
    // way the checkbox list this replaced behaved.
    const back = e.target.closest('[data-fback]');
    if (back) { e.stopPropagation(); openKey = null; renderMenu(); return; }
    // Checked before [data-fdim] because a value row carries both.
    const val = e.target.closest('[data-fval]');
    if (val) {
      e.stopPropagation();
      toggle(val.dataset.fdim, val.dataset.fval);
      apply();
      return;
    }
    const dim = e.target.closest('[data-fdim]');
    if (dim) { e.stopPropagation(); openKey = dim.dataset.fdim; renderMenu(); return; }
  });

  // Reopening the popover lands on the dimension list rather than wherever it was left.
  const trigger = chipsEl.querySelector('[data-menu]');
  if (trigger) trigger.addEventListener('click', () => { openKey = null; renderMenu(); });

  render();

  return { matches, count, entries, setFrom, clear, toggle, has, selected, render, apply };
}
