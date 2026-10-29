/* EPG Manager (templates/epg_manager.html) and the Maintenance page's EPG scan card
   (dev/changelog/1325). One thing happens here: Scan now asks the server to start the
   similarity scan, and while one is running the page polls its status and, when it ends,
   re-renders from the server - the result is server-rendered, so nothing is patched in.

   Every [data-act="epg-scan"] button starts a scan; a page that carries #em-main swaps
   it from the server when the scan ends, and one that carries #epg-scan-card (Maintenance)
   draws the latest scan into it from /api/epg-manager/scan. */
(() => {
  const POLL_MS = 3000;
  const STATUS = statusVocab('EPG_SCAN');
  let timer = null;

  const buttons = () => [...document.querySelectorAll('[data-act="epg-scan"]')];
  const setBusy = (busy) => buttons().forEach((b) => {
    b.disabled = busy;
    b.textContent = busy ? 'Scanning...' : 'Scan now';
  });

  // The Maintenance card: the latest scan's state, every status named.
  const card = () => document.getElementById('epg-scan-card');
  const renderCard = (scan) => {
    const el = card();
    if (!el) return;
    const n = (v) => (v === null || v === undefined ? '--' : Number(v).toLocaleString());
    let pill;
    if (!scan) pill = ['warn', 'NEVER RUN'];
    else if (scan.status === STATUS.RUNNING) pill = ['warn', 'SCANNING'];
    else if (scan.status === STATUS.FAILED) pill = ['bad', 'FAILED'];
    else if (scan.status === STATUS.SUCCESS) pill = ['ok', 'DONE'];
    else pill = ['warn', scan.status];
    const when = !scan ? 'never run'
      : scan.status === STATUS.RUNNING ? `started ${escHtml(scan.started_at)}`
        : escHtml(scan.finished_at || scan.started_at);
    const found = scan && scan.status === STATUS.SUCCESS
      ? `${n(scan.schedules)} schedules, ${n(scan.clusters)} carried by two or more ids, ${n(scan.links)} related pairs`
      : '';
    const err = scan && scan.error ? `<span class="ix-why bad">Error: ${escHtml(scan.error)}</span>` : '';
    el.innerHTML = `<div class="ix-row">
      <span class="ix-name">Similarity scan</span>
      <span class="h-mini ${pill[0]}">${escHtml(pill[1])}</span>
      <span class="ix-meta">${when}</span>
      <span class="ix-meta">${scan && scan.elapsed_ms !== null && scan.elapsed_ms !== undefined ? fmtDur(scan.elapsed_ms / 1000) : '--'}</span>
      <span class="ix-meta">${escHtml(found)}</span>
      ${err}
    </div>`;
  };

  const finished = async () => {
    setBusy(false);
    const main = document.getElementById('em-main');
    if (main) {
      try {
        await swapFromServer(['#em-main']);
      } catch (e) {
        showToast(`Could not reload the result: ${e.message}`, { type: 'error' });
      }
    }
  };

  const poll = async () => {
    let data;
    try {
      data = await jsonFetch('/api/epg-manager/scan');
    } catch (e) {
      return;   // a missed poll is retried on the next tick
    }
    renderCard(data.scan);
    const running = !!data.scan && data.scan.status === STATUS.RUNNING;
    setBusy(running);
    if (!running) {
      clearInterval(timer);
      timer = null;
      await finished();
    }
  };

  const watch = () => {
    if (timer === null) timer = setInterval(poll, POLL_MS);
  };

  document.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-act="epg-scan"]');
    if (!btn) return;
    setBusy(true);
    try {
      await jsonFetch('/api/epg-manager/scan', { method: 'POST', body: '{}' });
      showToast('Scan started. About a minute on a full guide.', { type: 'info' });
      watch();
    } catch (e) {
      showToast(e.message, { type: 'error' });
      setBusy(false);
    }
  });

  document.addEventListener('DOMContentLoaded', () => {
    const main = document.getElementById('em-main');
    if (main && main.dataset.running === '1') watch();
    if (card()) poll();
  });
})();
