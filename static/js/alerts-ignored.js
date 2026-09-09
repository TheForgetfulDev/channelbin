/* Ignored-alert-pattern management (templates/alerts_ignored.html). */
(() => {
  'use strict';

  const list = document.getElementById('ignored-list');

  function refreshCount() {
    const cnt = document.querySelector('.card-head .cnt');
    const sub = document.querySelector('.page-head .sub');
    if (!list) return;
    const n = list.querySelectorAll('tr').length;
    if (cnt) cnt.textContent = n;
    if (sub) sub.textContent = `${n} pattern${n === 1 ? '' : 's'}`;
    if (n === 0) location.reload();
  }

  function remove(row, id) {
    jsonFetch(`/api/alerts/ignored/${id}/remove`, { method: 'POST' })
      .then(() => { row.remove(); refreshCount(); })
      .catch((e) => showToast(`Could not remove that pattern: ${e.message}`, { type: 'error' }));
  }

  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act="remove"]');
    if (!btn) return;
    const row = btn.closest('tr');
    if (!row) return;
    remove(row, btn.dataset.id);
  });
})();
