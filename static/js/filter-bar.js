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

   Rollout: dev/changelog/767. */

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
    onChange();
  }

  function render() {
    prune();
    renderChips();
    renderMenu();
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
