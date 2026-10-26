/* Get started (dev/changelog/1280): moves the page on without a reload.

   The steps are server-rendered from app/setup_steps.py::page(), so this file never decides
   a step's state - it only re-fetches the page and swaps the regions when the nav poll says
   a fact behind them moved. The two signatures it compares against are read back out of the
   rendered #gs-live, the dashboard's pattern (static/js/dashboard.js), so a change landing
   between the render and the first poll is caught rather than remembered past.

   The sync signature is what keeps the first-sync step's "moves on by itself" promise; the
   recording signature moves the record step when a recording is scheduled from another tab.
   While a step is waiting (data-waiting), the minute tick also re-renders, so "first sync
   starts in 4m" counts down. */
(() => {
  const REGIONS = ['#gs-sub', '#gs-live'];
  const TICK_MS = 60 * 1000;
  let renderedAt = Date.now();
  let swapping = false;

  const live = () => document.getElementById('gs-live');

  function refresh() {
    if (swapping) return;
    swapping = true;
    swapFromServer(REGIONS)
      .then(() => { renderedAt = Date.now(); })
      .catch((err) => console.warn('Get started refresh failed; the next poll retries.', err))
      .finally(() => { swapping = false; });
  }

  function onSignature(key, sig) {
    const el = live();
    if (!el || typeof sig !== 'string') return;
    if (sig !== el.dataset[key]) { refresh(); return; }
    if (document.hidden || !el.dataset.waiting) return;
    if (Date.now() - renderedAt >= TICK_MS) refresh();
  }

  window.__applyAccountSync = (sig) => onSignature('syncSig', sig);
  window.__applyRecordingSignature = (sig) => onSignature('recSig', sig);
})();
