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

  const list = document.getElementById('alerts-list');

  // The nav badge belongs to base.html's poller. Calling ITS updater rather than
  // writing a second one is what keeps one updater per DOM region: this only
  // makes the count move on the click instead of at the next poll. The alert
  // banner is not rendered on this page, so applyAlerts returns right after the
  // count and the rail pip.
  const refreshCount = () => jsonFetch('/api/alerts/unread_count')
    .then((d) => {
      const el = document.getElementById('al-unread');
      if (el) el.textContent = `${d.count} unread`;
      if (window.__applyAlerts) window.__applyAlerts({ count: d.count });
    })
    .catch(() => { /* cosmetic - the action this followed already reported itself */ });

  // Rows leave as they are dismissed, so the card's own count is recomputed from
  // what is actually on screen; a server-rendered total left in place would keep
  // claiming rows that are gone.
  function refreshRowCount() {
    const cnt = document.querySelector('.card-head .cnt');
    if (!list || !cnt) return;
    const n = list.querySelectorAll('.al-row').length;
    cnt.textContent = n;
    // An emptied list has no empty state of its own - the server renders that
    // branch - so re-read the page rather than leaving a card with nothing in it.
    if (n === 0) location.reload();
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
            'they stay reachable under <strong>Show dismissed</strong>, and unread alerts are ' +
            'left alone.</p>',
      footer: [
        { label: 'Cancel', class: 'btn' },
        {
          label: 'Dismiss read alerts',
          class: 'btn btn-danger',
          onClick: (close) => {
            close();
            jsonFetch('/api/alerts/dismiss_all', { method: 'POST' })
              .then(() => {
                document.querySelectorAll('.al-row:not(.unread)').forEach((row) => row.remove());
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
