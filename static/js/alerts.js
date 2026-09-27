/* Alert center (templates/alerts.html).
   Rollout: dev/changelog/446.

   Lifted out of the template, where it lived as an inline var + string-concat
   block. Row actions arrive through one delegated listener, because the rows they
   act on are replaced whenever the page re-renders itself.

   Every request path reports its own failure. The version this replaced ended
   each call with `.catch(function() {})`, so a failed Dismiss removed nothing and
   said nothing - the user's only evidence was that the row was still there. That
   is the silence CLAUDE.md's principle 1 exists to forbid (BUGS.md 2026-08-03). */
(() => {
  'use strict';

  // The page renders two cards (dev/changelog/932). Active alerts are problems still
  // happening, which the app clears by itself, so no row there offers Dismiss - and the
  // dismiss routes refuse one anyway, so a stale page cannot hide a live problem either.
  //
  // ── Live refresh (dev/changelog/1131) ─────────────────────────────────────
  // #al-live (both cards and the empty state) and #al-unread have ONE writer: refresh(),
  // which swaps in the server's fresh copy. No action edits a row itself any more - each
  // one asks for a refresh once the server has answered, so a card's rows, its count and
  // the empty state are always the template's, never a client-side guess at them.
  // Triggers, the shape accounts.js uses:
  //   1. base.html's /api/nav-status poll hands over routes/alerts.py::alert_signature(),
  //      and it differs from the one #al-live was rendered at - an alert was raised,
  //      re-raised, read, dismissed or cleared itself, here or anywhere else;
  //   2. the render is a minute old, so "4m 10s ago" does not quietly drift. Skipped in a
  //      background tab, where nobody reads it;
  //   3. one of this page's own actions succeeded.
  // A poll-driven refresh waits while a row's menu is open - swapping the row out from under
  // it would close it mid-choice - and the next poll asks again. An open "Show details" does
  // not hold it (a disclosure left open would freeze the page for as long as it stayed
  // open); the swap re-opens it instead.
  const LIVE_REGIONS = ['#al-live', '#al-unread'];
  const RERENDER_MS = 60 * 1000;
  let renderedAt = Date.now();
  let inFlight = false;
  let again = false;

  const menuOpen = () => Boolean(document.querySelector('#al-live .menu.open'));

  function refresh() {
    if (inFlight) { again = true; return Promise.resolve(); }
    inFlight = true;
    const open = Array.from(document.querySelectorAll('#al-live .al-detail:not([hidden])'))
      .map((d) => d.closest('.al-row')?.dataset.id).filter(Boolean);
    return swapFromServer(LIVE_REGIONS)
      .then(() => {
        renderedAt = Date.now();
        open.forEach((id) => {
          const btn = document.querySelector(`#alert-${id} [data-act="detail"]`);
          if (btn) toggleDetail(btn);
        });
      })
      .catch((err) => console.warn('Alerts page refresh failed; the next poll retries.', err))
      .finally(() => {
        inFlight = false;
        // An action that landed mid-swap may have been read before the server wrote it.
        if (again) { again = false; refresh(); }
      });
  }

  window.__applyAlertSignature = (sig) => {
    const live = document.getElementById('al-live');
    if (!live || typeof sig !== 'string' || inFlight || menuOpen()) return;
    const changed = sig !== live.dataset.alertSig;
    if (!changed && (Date.now() - renderedAt < RERENDER_MS || document.hidden)) return;
    refresh();
  };

  // The nav counts belong to static/js/nav-alerts.js. Calling ITS updater rather than
  // writing a second one is what keeps one updater per DOM region: this only makes the
  // counts move on the click instead of at the next poll. The payload carries no banner
  // (this page renders none), so only the counts, the rail pip and its tip move.
  const refreshCount = () => jsonFetch('/api/alerts/unread_count')
    .then((d) => {
      if (window.__applyAlerts) window.__applyAlerts(d);
      if (window.__applyRailTips) window.__applyRailTips();
    })
    .catch(() => { /* cosmetic - the action this followed already reported itself */ });

  const afterAction = () => { refresh(); refreshCount(); };

  function markRead(row) {
    jsonFetch(`/api/alerts/${row.dataset.id}/read`, { method: 'POST' })
      .then(afterAction)
      .catch((e) => showToast(`Could not mark that alert read: ${e.message}`, { type: 'error' }));
  }

  function dismiss(row) {
    jsonFetch(`/api/alerts/${row.dataset.id}/dismiss`, { method: 'POST' })
      .then(afterAction)
      .catch((e) => showToast(`Could not dismiss that alert: ${e.message}`, { type: 'error' }));
  }

  function ignore(row) {
    const title = row.querySelector('.al-title')?.textContent || 'this alert';
    confirmIgnoreAlert(row.dataset.id, title, afterAction);
  }

  function toggleDetail(btn) {
    const detail = btn.parentElement.querySelector('.al-detail');
    if (!detail) return;
    detail.hidden = !detail.hidden;
    btn.textContent = detail.hidden ? 'Show details' : 'Hide details';
  }

  function dismissAll() {
    // Verb-named buttons, per DESIGN.md 4 - "OK" said nothing about what was
    // about to happen to which alerts. A footer button carrying an onClick does
    // not auto-close, so the action closes the modal itself.
    buildModal({
      title: 'Dismiss all read alerts',
      body: '<p>Alerts you have already read will be hidden from this page. Nothing is deleted - ' +
            'they stay reachable under <strong>Show dismissed</strong>. Unread alerts are left ' +
            'alone, and so is anything under <strong>Active alerts</strong>: those are problems ' +
            'that are still happening, and the app clears them itself once they are fixed.</p>',
      footer: [
        { label: 'Cancel', class: 'btn' },
        {
          label: 'Dismiss read alerts',
          class: 'btn btn-danger',
          onClick: (close) => {
            close();
            jsonFetch('/api/alerts/dismiss_all', { method: 'POST' })
              .then(afterAction)
              .catch((e) => showToast(`Could not dismiss the read alerts: ${e.message}`,
                                      { type: 'error' }));
          },
        },
      ],
    });
  }

  function readAll() {
    jsonFetch('/api/alerts/read_all', { method: 'POST' })
      .then(afterAction)
      .catch((e) => showToast(`Could not mark the alerts read: ${e.message}`, { type: 'error' }));
  }

  document.addEventListener('click', (e) => {
    if (e.target.closest('#al-read-all')) { readAll(); return; }
    if (e.target.closest('#al-dismiss-all')) { dismissAll(); return; }
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const row = btn.closest('.al-row');
    if (!row) return;
    if (btn.dataset.act === 'detail') toggleDetail(btn);
    else if (btn.dataset.act === 'read') markRead(row);
    else if (btn.dataset.act === 'ignore') ignore(row);
    else if (btn.dataset.act === 'dismiss') dismiss(row);
  });
})();
