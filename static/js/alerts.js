/* Alert center (templates/alerts.html).
   Rollout: dev/changelog/446.

   Lifted out of the template, where it lived as an inline var + string-concat
   block. Row actions arrive through one delegated listener, because the rows they
   act on are removed and re-read as the user works the page.

   Every request path reports its own failure. The version this replaced ended
   each call with `.catch(function() {})`, so a failed Dismiss removed nothing and
   said nothing - the user's only evidence was that the row was still there. That
   is the silence CLAUDE.md's principle 1 exists to forbid (BUGS.md 2026-08-03). */
(() => {
  'use strict';

  // The page renders two cards (dev/changelog/932). Active alerts are problems still
  // happening, which the app clears by itself, so no row there offers Dismiss - and the
  // dismiss routes refuse one anyway, so a stale page cannot hide a live problem either.
  const CARDS = [['alerts-active', 'al-card-active'], ['alerts-past', 'al-card-past']];

  // The nav counts belong to static/js/nav-alerts.js. Calling ITS updater rather than
  // writing a second one is what keeps one updater per DOM region: this only makes the
  // counts move on the click instead of at the next poll. The payload carries no banner
  // (this page renders none), so only the counts, the rail pip and its tip move.
  const refreshCount = () => jsonFetch('/api/alerts/unread_count')
    .then((d) => {
      const el = document.getElementById('al-unread');
      if (el) el.textContent = `${d.count} unread`;
      if (window.__applyAlerts) window.__applyAlerts(d);
      if (window.__applyRailTips) window.__applyRailTips();
    })
    .catch(() => { /* cosmetic - the action this followed already reported itself */ });

  // Rows leave as they are dismissed, so each card's own count is recomputed from
  // what is actually on screen; a server-rendered total left in place would keep
  // claiming rows that are gone. A card that empties hides itself - the server only
  // renders a card that has rows, so an empty one on screen is a client-side artifact.
  function refreshRowCount() {
    let total = 0;
    CARDS.forEach(([listId, cardId]) => {
      const rows = document.getElementById(listId);
      const card = document.getElementById(cardId);
      if (!rows || !card) return;
      const n = rows.querySelectorAll('.al-row').length;
      const cnt = card.querySelector('.card-head .cnt');
      if (cnt) cnt.textContent = n;
      card.hidden = n === 0;
      total += n;
    });
    // Nothing left in either card has no empty state of its own - the server renders
    // that branch - so re-read the page rather than leaving two empty cards.
    if (total === 0) location.reload();
  }

  function markRowRead(row) {
    row.classList.remove('unread');
    const item = row.querySelector('[data-act="read"]');
    if (item) item.remove();
  }

  function markRead(row) {
    jsonFetch(`/api/alerts/${row.dataset.id}/read`, { method: 'POST' })
      .then(() => { markRowRead(row); return refreshCount(); })
      .catch((e) => showToast(`Could not mark that alert read: ${e.message}`, { type: 'error' }));
  }

  function dismiss(row) {
    jsonFetch(`/api/alerts/${row.dataset.id}/dismiss`, { method: 'POST' })
      .then(() => { row.remove(); refreshRowCount(); return refreshCount(); })
      .catch((e) => showToast(`Could not dismiss that alert: ${e.message}`, { type: 'error' }));
  }

  function ignore(row) {
    const title = row.querySelector('.al-title')?.textContent || 'this alert';
    confirmIgnoreAlert(row.dataset.id, title, () => { row.remove(); refreshRowCount(); refreshCount(); });
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
              .then(() => {
                // Scoped to the Past card, matching what the route actually dismissed: a
                // sweep over every row would take the still-happening ones off the screen
                // while they sat untouched in the database, until the next page load put
                // them back with no explanation.
                document.querySelectorAll('#alerts-past .al-row:not(.unread)')
                  .forEach((row) => row.remove());
                refreshRowCount();
                return refreshCount();
              })
              .catch((e) => showToast(`Could not dismiss the read alerts: ${e.message}`,
                                      { type: 'error' }));
          },
        },
      ],
    });
  }

  function readAll() {
    jsonFetch('/api/alerts/read_all', { method: 'POST' })
      .then(() => {
        document.querySelectorAll('.al-row.unread').forEach(markRowRead);
        return refreshCount();
      })
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
