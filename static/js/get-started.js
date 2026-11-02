/* Get started (dev/changelog/1280): moves the page on without a reload.

   The steps are server-rendered from app/setup_steps.py::page(), so this file never decides
   a step's state - it only re-fetches the page and swaps the regions when the nav poll says
   a fact behind them moved. The two signatures it compares against are read back out of the
   rendered #gs-live, the dashboard's pattern (static/js/dashboard.js), so a change landing
   between the render and the first poll is caught rather than remembered past.

   The sync signature is what keeps the first-sync step's "moves on by itself" promise; the
   recording signature moves the record step when a recording is scheduled from another tab.
   While a step is waiting (data-waiting), the minute tick also re-renders, so "first sync
   starts in 4m" counts down.

   #gs-activity, the example channels' progress card (dev/changelog/1396), is a region of its
   own with its own poll of /get-started/activity, which renders just the card: re-rendering
   the whole page every few seconds would re-run every Readiness check. When a load's step
   moves, the steps under it may have too, so #gs-live is refreshed then as well. */
(() => {
  const REGIONS = ['#gs-sub', '#gs-live'];
  const TICK_MS = 60 * 1000;
  const ACTIVITY_URL = '/get-started/activity';
  const ACTIVITY_MS = 2500;
  let renderedAt = Date.now();
  let swapping = false;
  let activityTimer = null;

  const live = () => document.getElementById('gs-live');
  const activity = () => document.getElementById('gs-activity');

  function refresh() {
    if (swapping) return Promise.resolve();
    swapping = true;
    return swapFromServer(REGIONS)
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

  // Swaps the card's contents and data attributes but keeps the #gs-activity element itself,
  // which holds focus once a load starts; swapFromServer replaces the element, and the focus
  // went with it on the first poll.
  async function swapActivity() {
    const el = activity();
    const res = await fetch(ACTIVITY_URL, { cache: 'no-store' });
    if (!res.ok) throw new Error(`${ACTIVITY_URL} answered ${res.status}`);
    const fresh = new DOMParser().parseFromString(await res.text(), 'text/html')
      .getElementById('gs-activity');
    if (!el || !fresh) return;
    ['sig', 'running'].forEach((k) => {
      if (fresh.dataset[k] === undefined) delete el.dataset[k];
      else el.dataset[k] = fresh.dataset[k];
    });
    el.innerHTML = fresh.innerHTML;
  }

  async function pollActivity() {
    activityTimer = null;
    const before = activity()?.dataset.sig;
    try {
      await swapActivity();
    } catch (err) {
      console.warn('Example channels progress refresh failed; retrying.', err);
    }
    const el = activity();
    if (el && el.dataset.sig !== before) refresh();
    scheduleActivity();
  }

  function scheduleActivity() {
    if (activityTimer === null && activity()?.dataset.running) {
      activityTimer = setTimeout(pollActivity, ACTIVITY_MS);
    }
  }

  // examples.js calls this once the load has started: draw the card, bring it into view,
  // and follow it from there.
  window.__examplesStarted = async () => {
    await swapActivity();
    const el = activity();
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      el.focus({ preventScroll: true });
    }
    refresh();
    scheduleActivity();
  };

  window.__applyAccountSync = (sig) => onSignature('syncSig', sig);
  window.__applyRecordingSignature = (sig) => onSignature('recSig', sig);
  scheduleActivity();
})();
