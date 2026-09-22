/* The one dropdown component (DESIGN.md 15.3).

   Five controls across Settings and Notifications are "open a list, pick from
   it": the routing table's `Push to`, `Add a service`, `Start from an example`
   and the two tag-cleanup pickers. They are ONE component, so the trigger label,
   the dismissal and the clipping behavior cannot drift apart. A sixth dropdown
   is a registry entry, not a sixth implementation.

   It is deliberately NOT a second portal. The popover is a single node carrying
   `.menu` as well as `.msel-pop`, so util.js already owns every part of it that
   has app-wide rules: positionMenu() clamps it horizontally and flips it above
   the trigger near the bottom of the window (DESIGN.md 10.4), closeMenus()
   dismisses it, the outside-click / Escape / scroll handlers reach it, and
   OVERLAY_SEL's `.menu.open` gives it the scroll lock. One node also means the
   21-row routing table carries no hidden menus at all, and that a menu in its
   last row escapes `.table-scroll` (overflow-x: auto below 961px) instead of
   being cut off by it.

   Rollout: dev/changelog/440.

   A definition is:
     { title:  arg => 'Heading of the popover',
       rows:   arg => [{v, label, sub?, dot?, right?, mono?}],
       on:     arg => [selected values],        // multi-select only
       label:  arg => 'text for the trigger',
       toggle: (arg, value, checked) => {},     // multi-select only
       single: true, pick: (arg, value) => {} } // single-pick instead

   `key` is `id` or `id:arg`, so 21 routing rows are 21 triggers over one
   definition rather than 21 definitions. */

const _DROPDOWNS = {};
const DD_POP_ID = 'dd-pop';

/* `id:arg` -> ['id', 'arg']; a bare id -> ['id', '']. Split on the FIRST colon
   only: an alert type or a service key may itself contain one. */
function ddKeyParts(key) {
  const i = String(key).indexOf(':');
  return i === -1 ? [String(key), ''] : [String(key).slice(0, i), String(key).slice(i + 1)];
}

/* `None` / the one name / `N things`. Always re-derived from state, never patched
   at the click site, so a trigger can never disagree with the boxes inside it. */
function ddPickLabel(names, noun) {
  if (!names.length) return 'None';
  return names.length === 1 ? names[0] : `${names.length} ${noun}`;
}

/* `prefix` exists for `Add a service`: syncDropdownTriggers rewrites .mlbl from
   the definition's label(), so a `+` glyph baked into that text is erased by the
   next sync. It travels as data-mpre and is re-applied there instead
   (DESIGN.md 15.7). */
function dropdownTriggerHtml(key, extraClass = '', prefix = '') {
  const [id, arg] = ddKeyParts(key);
  const def = _DROPDOWNS[id];
  const label = def ? def.label(arg) : '';
  return `<button class="btn btn-sm msel${extraClass ? ' ' + extraClass : ''}" type="button" ` +
    `data-msel="${escHtml(key)}"${prefix ? ` data-mpre="${escHtml(prefix)}"` : ''}>` +
    `<span class="mlbl">${escHtml(prefix + label)}</span><span aria-hidden="true">&#9662;</span></button>`;
}

/* One writer for every trigger label on the page. */
function syncDropdownTriggers(root = document) {
  root.querySelectorAll('[data-msel]').forEach((btn) => {
    const [id, arg] = ddKeyParts(btn.dataset.msel);
    const def = _DROPDOWNS[id];
    const lbl = btn.querySelector('.mlbl');
    if (def && lbl) lbl.textContent = (btn.dataset.mpre || '') + def.label(arg);
  });
}

function closeDropdown() {
  const pop = document.getElementById(DD_POP_ID);
  if (pop && pop.classList.contains('open')) closeMenus();
}

function _ddRowHtml(def, row, on) {
  if (def.single) {
    return `<button class="mopt mpick" type="button" data-mpick="${escHtml(row.v)}">` +
      `<span class="mo-t${row.mono ? ' mono' : ''}">${escHtml(row.label)}</span>` +
      (row.sub ? `<span class="mo-s">${escHtml(row.sub)}</span>` : '') + '</button>';
  }
  return `<label class="mopt"><input type="checkbox" data-mopt="${escHtml(row.v)}"` +
    `${on.includes(row.v) ? ' checked' : ''}>` +
    (row.dot ? `<span class="tagdot" style="background:${escHtml(row.dot)}"></span>` : '') +
    `<span>${escHtml(row.label)}</span>` +
    (row.right ? `<span class="pats">${escHtml(row.right)}</span>` : '') + '</label>';
}

function openDropdown(trigger) {
  const key = trigger.dataset.msel;
  const [id, arg] = ddKeyParts(key);
  const def = _DROPDOWNS[id];
  if (!def) return;
  // The portal is created on first use, here rather than in a helper of its own, so
  // the node that carries an overlay class is appended by the one unit that also
  // takes the scroll lock. .menu is what hands util.js ownership of positioning,
  // dismissal and that lock; .pop-left prefers aligning to the trigger's left edge.
  let pop = document.getElementById(DD_POP_ID);
  if (!pop) {
    pop = document.createElement('div');
    pop.id = DD_POP_ID;
    pop.className = 'menu msel-pop pop-left';
    document.body.appendChild(pop);
  }
  // Clicking the trigger of the open menu closes it, rather than rebuilding the
  // same list underneath the cursor.
  if (pop.classList.contains('open') && pop.dataset.for === key) { closeDropdown(); return; }
  closeDropdown();
  const rows = def.rows(arg);
  const on = def.single ? [] : def.on(arg);
  pop.innerHTML = `<div class="pop-title">${escHtml(def.title(arg))}</div>` +
    (rows.length
      ? rows.map((row) => _ddRowHtml(def, row, on)).join('')
      : '<div class="mo-s" style="padding:6px 9px">Nothing left to pick.</div>');
  pop.dataset.for = key;
  pop.classList.add('open');
  // Lock first, measure second: the lock reserves the scrollbar's width as body
  // padding, which moves the trigger. Measuring before it would leave the popover
  // a scrollbar's width off its button.
  syncScrollLock();
  positionMenu(pop, trigger);
}

let _ddWired = false;

/* Registering the first definition is what wires the listeners - a page that
   never registers one never pays for them. */
function registerDropdown(id, def) {
  _DROPDOWNS[id] = def;
  if (_ddWired) return;
  _ddWired = true;

  // Capture, and stopPropagation on a handled trigger: util.js's own bubble-phase
  // click handler closes every open menu on any click outside one, and it would
  // otherwise shut this popover in the same click that opened it.
  document.addEventListener('click', (e) => {
    const trigger = e.target.closest('[data-msel]');
    if (trigger && !trigger.disabled) {
      e.preventDefault();
      e.stopPropagation();
      openDropdown(trigger);
      return;
    }
    // A single-pick row acts and closes. Propagation is left alone here: by the
    // time util.js sees the click the popover is already closed, so its
    // inside-a-menu branch does not apply and its closeMenus() is a no-op.
    const pick = e.target.closest('[data-mpick]');
    if (pick) {
      const pop = document.getElementById(DD_POP_ID);
      const [pid, parg] = ddKeyParts((pop && pop.dataset.for) || '');
      closeDropdown();
      if (_DROPDOWNS[pid]) _DROPDOWNS[pid].pick(parg, pick.dataset.mpick);
      syncDropdownTriggers();
    }
  }, true);

  // A ticked row. `change` rather than `click` because a label-activated checkbox
  // reports the pre-toggle state during the click. The definition owns what the
  // tick means; this only re-derives every trigger label afterwards. The popover
  // stays open (util.js keeps non-action clicks inside a menu from dismissing it),
  // so several boxes can be ticked in one visit.
  document.addEventListener('change', (e) => {
    const opt = e.target.closest('[data-mopt]');
    if (!opt) return;
    const pop = document.getElementById(DD_POP_ID);
    const [id2, arg2] = ddKeyParts((pop && pop.dataset.for) || '');
    if (_DROPDOWNS[id2]) _DROPDOWNS[id2].toggle(arg2, opt.dataset.mopt, opt.checked);
    syncDropdownTriggers();
  });
}
