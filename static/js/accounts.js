/* The Accounts list (templates/accounts.html, DESIGN.md §17.1/§17.2/§17.5).

   Rows are server-rendered; this file adds the four things the list does:

   1. **The row is the click target for the account page** (§3.9), with the recordings
      list's guard: the actions cell and any real link swallow their own clicks. Nothing in
      a row may stopPropagation on the way up - the kebab handler is delegated on document
      in util.js, so a row that swallowed the event would stop the menu from ever opening.

   2. **Every action goes through the shared helpers in account-actions.js**, which is also
      what the account page uses. The two surfaces offer the same list, so they run the same
      code - including the conflict override, which is the thing most easily lost.

   3. **On a phone the kebab becomes a bottom sheet** (§17.5 item 2), and the sheet is BUILT
      FROM THE ROW'S OWN MENU. That makes "the same list, in the same order" structural
      rather than a promise: there is one place the actions are written down, the template.
      The sheet's markup is dropped when it closes, so a closed sheet can never leave a
      second, invisible copy of an action aimed at whichever account it was last opened on
      (§17.6). The interception is capture-phase, which is what lets it run before util.js's
      own document-level menu handler rather than depending on script order.

   4. **The list keeps itself current** (dev/changelog/921) by swapping in a fresh server
      render, never by patching rows from JSON - the template stays the only thing that
      knows how a row looks. Because the rows are replaced wholesale, every handler above is
      delegated on document; a listener bound to a row would go with the row.

   Depends on util.js (escHtml, buildModal, closeMenus, swapFromServer), account-actions.js
   and account-modal.js (openAccountModal).
*/
(() => {
  'use strict';

  // One spelling of 768 in this file, matching the one in style.css (the pattern
  // channel-search.js and dashboard.js already follow).
  const isPhone = () => window.matchMedia('(max-width: 768px)').matches;
  const reload = () => window.location.reload();

  // ── Row navigation (§3.9) ────────────────────────────────────────────────
  // A real link inside the row (Browse channels, the name itself) navigates on its
  // own; the actions cell is the row's one dead zone. [data-tip] is excluded the
  // same way index.html's recordings-list NO_NAV does it: hovering a tooltip target shows
  // the tooltip, clicking it does nothing, and the rest of the row still navigates.
  const NO_NAV = '.a-actions, .menu, a, [data-tip]';
  document.addEventListener('click', (e) => {
    const row = e.target.closest('.acct-rows .arow');
    if (!row || e.target.closest(NO_NAV)) return;
    window.location = row.dataset.href;
  });

  // ── Actions ──────────────────────────────────────────────────────────────
  function run(act, row) {
    const id = row.dataset.id;
    const hooks = { onDone: reload };
    switch (act) {
      case 'sync':
        accountSync(id, hooks);
        return;
      case 'cancel-sync':
        accountCancelSync(id, hooks);
        return;
      case 'force-epg':
        confirmForceEpgResync(id, hooks);
        return;
      case 'settings':
        // The SAME modal the account page opens - it takes an account id and nothing else.
        openAccountModal({ accountId: id, onDone: reload });
        return;
      case 'delete':
        confirmDeleteAccount({
          id: id,
          name: row.dataset.name,
          channels: row.dataset.channels,
          epg: row.dataset.epg,
          onDone: reload,
        });
        return;
      default:
        // Named rather than ignored: an action the template offers and this file does not
        // handle is a button that does nothing, which must not be silent.
        console.warn('unhandled account row action', act);
    }
  }

  document.addEventListener('click', (e) => {
    const item = e.target.closest('.arow .menu-item[data-act]');
    if (!item || item.disabled) return;
    e.preventDefault();
    closeMenus();
    run(item.dataset.act, item.closest('.arow'));
  });

  // ── The phone's bottom sheet (§17.5 item 2) ──────────────────────────────
  function sheetItems(menu) {
    return Array.from(menu.children).map((el) => {
      if (el.classList.contains('sep')) return '<div class="sheet-sep"></div>';
      const label = escHtml(el.textContent.trim());
      if (el.tagName === 'A') {
        return `<a class="sheet-act" href="${escHtml(el.getAttribute('href'))}">${label}</a>`;
      }
      return `<button class="sheet-act${el.classList.contains('danger') ? ' danger' : ''}" ` +
        `data-act="${escHtml(el.dataset.act || '')}"${el.disabled ? ' disabled' : ''}>${label}</button>`;
    }).join('');
  }

  function openRowSheet(row) {
    const menu = row.querySelector('.menu');
    if (!menu) return;
    const body = document.createElement('div');
    body.className = 'acct-sheet';
    body.innerHTML = sheetItems(menu);
    const sheet = buildModal({ title: row.dataset.name, body });
    body.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn || btn.disabled) return;
      // Close first: the sheet's markup goes with it, and the action then runs against a
      // document holding exactly one copy of everything.
      sheet.closeModal();
      run(btn.dataset.act, row);
    });
  }

  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.arow [data-sheet]');
    if (!btn || !isPhone()) return;
    // Capture phase: stopping here means util.js's document-level handler never opens the
    // popover, so the two can never both be on screen.
    e.preventDefault();
    e.stopPropagation();
    openRowSheet(btn.closest('.arow'));
  }, true);

  // ── Live refresh (dev/changelog/921) ─────────────────────────────────────
  // Driven by base.html's /api/nav-status poll rather than a timer of its own, which hands
  // over accounts.sync_signature(). Two triggers, and each covers what the other cannot:
  //   1. the signature differs from the one #acct-live was rendered at - a sync started,
  //      finished, failed or was cancelled since. This is what stops a finished sync reading
  //      SYNCING until somebody reloads;
  //   2. the render is a minute old, so "Synced 4m ago" and "in 23h 44m" do not quietly
  //      drift while nothing changes. Skipped in a background tab, where nobody reads them.
  // A refresh waits while a row's menu or phone sheet is open - swapping the row out from
  // under an open menu would close it mid-choice - and the next poll simply asks again.
  const LIVE_REGIONS = ['#acct-live', '#acct-total'];
  const RERENDER_MS = 60 * 1000;
  let renderedAt = Date.now();
  let inFlight = false;

  const busy = () => Boolean(document.querySelector('.acct-rows .menu.open, .acct-sheet'));

  window.__applyAccountSync = (sig) => {
    const live = document.getElementById('acct-live');
    if (!live || typeof sig !== 'string' || inFlight || busy()) return;
    const changed = sig !== live.dataset.syncSig;
    if (!changed && (Date.now() - renderedAt < RERENDER_MS || document.hidden)) return;
    inFlight = true;
    swapFromServer(LIVE_REGIONS)
      .then(() => { renderedAt = Date.now(); })
      .catch((err) => console.warn('Accounts list refresh failed; the next poll retries.', err))
      .finally(() => { inFlight = false; });
  };
// The call parens go OUTSIDE the wrapper, unlike account-detail.js's `(function () {}())`.
// An arrow function is not a valid callee inside the group, so `(() => {}())` is a syntax
// error that takes the whole file with it - nothing on this page would have run.
})();
