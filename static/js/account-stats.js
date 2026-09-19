/* The account stats (templates/_account_stats.html, DESIGN.md §17.7, dev/changelog/1029).

   The section is server-rendered and has exactly one renderer, the template: this file
   never builds a number. It adds three behaviors, every one delegated on document so it
   works on any page that includes the macros:

   1. **The window chips** are real links (`?w=`). A plain click saves the pick to the
      user-prefs store first, so every page that shows these stats opens on it next time,
      then follows the link; a Ctrl/Cmd/middle click is left to the browser.
   2. **The comparison table sorts** on any column, both directions (a second click
      reverses). The totals row is the table's footing and never moves; an account with no
      pass rate sorts last either way, as the group page sorts untested channels. On a phone
      the header row is gone, so the Sort chip's menu drives the same function.
   3. **The trend column tooltip.** util.js's [data-tip] tooltip is plain text by design,
      and this one has to carry each account's color, so it uses the guide's .dot-tooltip
      surface: fixed, above the column, flipped below near the top, clamped to the
      viewport. Hover on a desktop, tap on a phone, focus from the keyboard.

   Depends on util.js (jsonFetch, followHref, wantsNewTab, closeMenus, escHtml).
*/
(() => {
  'use strict';

  const PREF_KEY = 'account_stats_window';

  // ── 1. The window ─────────────────────────────────────────────────────────
  document.addEventListener('click', (e) => {
    const chip = e.target.closest('.acst-win a[data-win]');
    if (!chip || e.button !== 0 || wantsNewTab(e)) return;
    e.preventDefault();
    const href = chip.getAttribute('href');
    // A failed save still navigates: the link carries the window itself, and only the
    // "open on this next time" half is lost - said in the console, not swallowed.
    jsonFetch(`/api/user-prefs/${PREF_KEY}`, {
      method: 'POST', body: JSON.stringify({ value: chip.dataset.win }),
    })
      .catch((err) => console.warn('Could not save the stats window; the link still applies it.', err))
      .finally(() => followHref(e, href));
  });

  // ── 2. The table sort ─────────────────────────────────────────────────────
  const LABELS = {};
  let sortKey = null;
  let sortDir = -1;

  function sortValue(tr, key) {
    if (key === 'name') return tr.dataset.name;
    const raw = tr.dataset[key];
    return raw === '' || raw === undefined ? null : Number(raw);
  }

  function applySort(table) {
    const body = table.tBodies[0];
    const total = body.querySelector('tr.acst-total');
    const rows = Array.from(body.querySelectorAll('tr[data-id]'));
    rows.sort((x, y) => {
      if (!sortKey) return Number(x.dataset.order) - Number(y.dataset.order);
      const a = sortValue(x, sortKey);
      const b = sortValue(y, sortKey);
      if (a === null && b === null) return 0;
      if (a === null) return 1;
      if (b === null) return -1;
      return (a < b ? -1 : a > b ? 1 : 0) * sortDir;
    });
    rows.forEach((tr) => body.insertBefore(tr, total));
    table.querySelectorAll('th[data-sort]').forEach((th) => {
      if (!LABELS[th.dataset.sort]) LABELS[th.dataset.sort] = th.firstChild.textContent.trim();
      const on = th.dataset.sort === sortKey;
      th.classList.toggle('sorted', on);
      th.setAttribute('aria-sort', on ? (sortDir < 0 ? 'descending' : 'ascending') : 'none');
      th.querySelector('.arr').textContent = on ? (sortDir < 0 ? ' ▾' : ' ▴') : '';
    });
    const label = document.querySelector('#acst-sort-chip .acst-sort-label');
    if (label) {
      label.textContent = sortKey
        ? `${LABELS[sortKey]} ${sortDir < 0 ? '▾' : '▴'}` : 'Account order';
    }
  }

  function setSort(key) {
    const table = document.getElementById('acst-table');
    if (!table) return;
    if (sortKey === key) {
      sortDir = -sortDir;
    } else {
      sortKey = key;
      // Names read A to Z first; numbers biggest first.
      sortDir = key === 'name' ? 1 : -1;
    }
    applySort(table);
  }

  document.addEventListener('click', (e) => {
    const el = e.target.closest('#acst-table th[data-sort], #acst-sort-chip + .menu [data-sort]');
    if (!el) return;
    closeMenus();
    setSort(el.dataset.sort);
  });

  // ── 3. The column tooltip ─────────────────────────────────────────────────
  let tip = null;
  let tipFor = null;

  function hideTip() {
    if (tip) tip.style.display = 'none';
    tipFor = null;
  }

  function showTip(col) {
    const plot = col.closest('.acst-plot');
    let t;
    try {
      t = JSON.parse(plot.dataset.tips)[Number(col.dataset.col)];
    } catch (err) {
      console.warn('Unreadable chart tooltip data', err);
      return;
    }
    if (!t) return;
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'dot-tooltip acst-tt';
      tip.setAttribute('role', 'tooltip');
      document.body.appendChild(tip);
    }
    const rows = t.rows.length
      ? t.rows.map((r) => '<div class="acst-tt-row">'
        + `<span class="stackbar-dot" style="background:${escHtml(r.color)}"></span>`
        + `<span class="tt-name">${escHtml(r.name)}</span><span class="acst-tt-v">${escHtml(r.value)}</span></div>`
        + (r.detail ? `<div class="tt-detail acst-tt-d">${escHtml(r.detail)}</div>` : '')).join('')
      : '<span class="tt-detail">Nothing</span>';
    const total = t.total
      ? `<div class="acst-tt-row acst-tt-tot"><span></span><span class="tt-name">Total</span><span class="acst-tt-v">${escHtml(t.total)}</span></div>`
      : '';
    tip.innerHTML = `<strong>${escHtml(t.title)}</strong>${rows}${total}`;
    tip.style.display = 'flex';
    const r = col.getBoundingClientRect();
    const left = Math.min(Math.max(8, r.left + r.width / 2 - tip.offsetWidth / 2),
      window.innerWidth - tip.offsetWidth - 8);
    let top = r.top - tip.offsetHeight - 8;
    if (top < 8) top = r.bottom + 8;
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
    tipFor = col;
  }

  document.addEventListener('mouseover', (e) => {
    const col = e.target.closest('.acst-col');
    if (col && col !== tipFor) showTip(col);
    else if (!col && tipFor) hideTip();
  });
  document.addEventListener('focusin', (e) => {
    const col = e.target.closest('.acst-col');
    if (col) showTip(col);
    else if (tipFor) hideTip();
  });
  // A tap shows the column's tooltip; a tap anywhere else closes it.
  document.addEventListener('click', (e) => {
    const col = e.target.closest('.acst-col');
    if (col) showTip(col);
    else if (tipFor) hideTip();
  });
  document.addEventListener('scroll', hideTip, true);
})();
