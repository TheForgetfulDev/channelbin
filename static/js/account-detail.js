/* The account page (templates/account_detail.html, DESIGN.md §17.3/§17.5).

   What lives here, and why each piece is shaped the way it is:

   1. **The sync history region has exactly ONE renderer** - `renderHistory()`, fed from
      `ACCOUNT_DETAIL.logs` (the first paint) or from /api/accounts/<id>/syncs (after "All
      N syncs"). Both sources are the same shape, produced by one server-side serializer,
      so a first paint and an expand cannot disagree about what a run looked like. There is
      no Jinja copy of a history row anywhere.

   2. **One markup, both breakpoints.** The row carries every column at every width; CSS
      reflows it to two lines on a phone with the error spanning full width. No column is
      dropped, because the error text is the only place a failed sync says WHY (§16.5 item
      5, §17.5 item 5). The desktop duration bar is scaled against the longest run SHOWN,
      and the header names that scale - a length nobody can explain is worse than none.

   3. **The sticky bottom bar** (§17.5 item 3) duplicates the inline action bar's primary,
      and both come from the same Jinja macro, so they can never offer different things.
      Four rules, each bought with a browser pass (§17.6):
        - it hides with `transform`, never `display:none` - an unmounted element cannot
          animate back;
        - `body.actbar-on` tracks the bar being VISIBLE, not present, so a short page that
          never shows it does not shove its toasts up for nothing;
        - the reserved bottom padding deliberately does NOT toggle with the bar - coupling
          them would let showing the bar change the page's scroll height underneath the
          very observer deciding whether to show it;
        - both fallbacks (no inline bar, no IntersectionObserver) resolve toward SHOWING
          it, so a primary action is never stranded off screen.

   Depends on util.js (escHtml, fmtDur, jsonFetch, showToast, buildModal, closeMenus),
   account-actions.js (accountSync, accountCancelSync, confirmForceEpgResync,
   confirmDeleteAccount - shared with the list page) and account-modal.js (openAccountModal).
*/
(function () {
  const A = window.ACCOUNT_DETAIL;
  if (!A) return;

  const $ = (sel, root = document) => root.querySelector(sel);

  // ── Formatting ───────────────────────────────────────────────────────────
  // Storage is naive UTC; display always goes through the configured timezone, never a
  // hardcoded one (CLAUDE.md Timezones). util.js owns both halves - parsing the naive-UTC
  // string back into an instant, and rendering it in the display timezone and clock format.
  const fmtWhen = (s) => {
    const d = utcIsoToDate(s);
    return d ? fmtDateTz(d, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
      : 'never';
  };
  const fmtNum = (n) => (n === null || n === undefined ? '--' : Number(n).toLocaleString('en-US'));

  // ── Action feedback ──────────────────────────────────────────────────────
  const errBox = $('#acct-action-error');
  function showActionError(msg) {
    if (!errBox) return;
    errBox.textContent = msg || '';
    errBox.style.display = msg ? '' : 'none';
  }

  function post(url, opts = {}) {
    showActionError('');
    return jsonFetch(url, opts)
      .then((res) => {
        showToast(res.message || 'Done.');
        return res;
      })
      .catch((e) => {
        // Never swallowed: an action that fails silently is the defect the Alerts page
        // shipped with (dev/docs/BUGS.md 2026-08-03 @ 07:43:07 PM ET).
        showActionError(e.message || 'Request failed.');
        showToast(e.message || 'Request failed.', { type: 'error' });
        throw e;
      });
  }

  const reload = () => { window.location.reload(); };

  // ── The sync history region ──────────────────────────────────────────────
  let logs = A.logs || [];
  let expanded = false;

  const LOG_BADGE = {
    SUCCESS: ['b-done', 'Success'],
    PARTIAL: ['b-warn', 'Partial'],
    ERROR: ['b-fail', 'Error'],
    CANCELLED: ['b-abort', 'Cancelled'],
    // A scheduled sync that never ran (app/accounts.py::record_skipped_sync). Neutral, not
    // an error badge: nothing failed, the occurrence yielded to a recording or to heavier
    // database work and the reason is on the row (dev/changelog/928).
    SKIPPED: ['b-paused', 'Skipped'],
  };
  function logBadge(status) {
    // b-running, not b-live: a sync in progress is not an alarm (dev/changelog/816).
    if (!status) return '<span class="badge b-running"><span class="pulse"></span>Running</span>';
    const hit = LOG_BADGE[status];
    // Every state is named; the fallthrough logs and renders the raw value rather than
    // quietly becoming the home for the next status anybody adds.
    if (!hit) {
      console.warn('unhandled sync log status', status);
      return `<span class="badge b-abort">${escHtml(status)}</span>`;
    }
    return `<span class="badge ${hit[0]}">${hit[1]}</span>`;
  }

  function historyRow(log, maxDuration) {
    const secs = log.duration_seconds;
    const barClass = log.status === 'ERROR' ? ' err' : (log.status === 'PARTIAL' ? ' part' : '');
    const width = secs === null || !maxDuration
      ? 0 : Math.max(2, Math.round((secs / maxDuration) * 90));
    const duration = secs === null
      ? '<span class="text-faint">running</span>'
      : `<span class="hist-bar"><span class="bar${barClass}" style="width:${width}px"></span>` +
        `<span class="mono">${escHtml(fmtDur(secs, false))}</span></span>`;
    return '<div class="hist-row">' +
      `<span class="h-when mono">${escHtml(fmtWhen(log.started_at))}</span>` +
      `<span class="h-res">${logBadge(log.status)}</span>` +
      `<span class="h-num"><span class="h-k">Channels</span>${log.channels ? fmtNum(log.channels) : '<span class="text-faint">--</span>'}</span>` +
      `<span class="h-num"><span class="h-k">EPG</span>${log.epg ? fmtNum(log.epg) : '<span class="text-faint">--</span>'}</span>` +
      `<span class="h-dur"><span class="h-k">Duration</span>${duration}</span>` +
      `<span class="h-err">${log.error ? escHtml(log.error) : '<span class="text-faint">--</span>'}</span>` +
      '</div>';
  }

  function renderHistory() {
    const box = $('#acct-hist');
    if (!box) return;
    if (!logs.length) return;
    const durations = logs.map((l) => l.duration_seconds).filter((d) => d !== null && d !== undefined);
    const maxDuration = durations.length ? Math.max(...durations) : 0;
    const head = '<div class="hist-head">' +
      '<span>Started</span><span>Result</span><span class="h-num">Channels</span>' +
      '<span class="h-num">EPG</span>' +
      `<span class="tip-plain" data-tip="How long the sync took.&#10;Bars are scaled against the longest run shown here, ${escHtml(fmtDur(maxDuration, false))}.">Duration</span>` +
      '<span>Error</span></div>';
    box.innerHTML = head + logs.map((l) => historyRow(l, maxDuration)).join('');
  }

  // "All N syncs" loads the rest into this same section and becomes "Show fewer" -
  // account_logs.html and its route are retired (§17.1), so there is no page to go to.
  const moreBtn = $('#acct-hist-more');
  if (moreBtn) {
    moreBtn.addEventListener('click', () => {
      if (expanded) {
        logs = (A.logs || []).slice();
        expanded = false;
        moreBtn.textContent = `All ${fmtNum(Number(moreBtn.dataset.total))} syncs`;
        renderHistory();
        return;
      }
      moreBtn.disabled = true;
      jsonFetch(`/api/accounts/${A.id}/syncs`)
        .then((res) => {
          logs = res.logs || [];
          expanded = true;
          moreBtn.textContent = 'Show fewer';
          renderHistory();
        })
        .catch((e) => showToast(e.message || 'Could not load the sync history.', { type: 'error' }))
        .finally(() => { moreBtn.disabled = false; });
    });
  }

  renderHistory();

  // ── Section layout (order + hidden), persisted server-side ───────────────
  const SEC_NAMES = { details: 'Details', content: 'Content', usage: 'Usage',
                      history: 'Sync history', activity: 'Activity' };
  const sectionLayout = initSectionLayout({
    config: A,
    saveUrl: '/api/user-prefs/account_detail_sections',
    names: SEC_NAMES,
    note: 'The title, status bar and warning banners always show.',
  });

  // ── Actions ──────────────────────────────────────────────────────────────
  // Sync, cancel, force-EPG and delete are the SHARED helpers in account-actions.js, the
  // same code the list row's kebab runs. They were written twice at first, and the second
  // copy is what lost the sync-conflict override on this page: DESIGN-concurrency.md §5.4
  // requires warn-and-override at every manual entry point, and this one only warned
  // (dev/docs/BUGS.md 2026-08-04). One implementation is the fix, not a tidier duplicate.
  // `onError` puts the reason in this page's own error line as well as the toast.
  const hooks = () => {
    showActionError('');
    return { onDone: reload, onError: showActionError };
  };

  const ACTIONS = {
    sync: () => accountSync(A.id, hooks()),
    'cancel-sync': () => accountCancelSync(A.id, hooks()),
    'force-epg': () => confirmForceEpgResync(A.id, hooks()),
    settings: () => openAccountModal({ accountId: A.id, onDone: reload }),
    sections: sectionLayout.open,
    dump: () => post(`/api/accounts/${A.id}/dump`, { method: 'POST' }),
    'sync-dump': () => post(`/api/accounts/${A.id}/sync-from-dump`, { method: 'POST' }).then(reload),
    // The account page knows its own sync count, so its confirm names all three totals;
    // the list row omits that clause rather than inventing a number it does not have.
    delete: () => confirmDeleteAccount(Object.assign(hooks(), {
      id: A.id,
      name: A.name,
      channels: A.channels,
      epg: A.epg,
      syncs: A.totalSyncs,
      onDone: () => { window.location.href = A.accountsUrl; },  // nav-ok: redirect after deleting the account
    })),
  };

  // The sticky bar's overflow is a SHEET, not a popover: a control pinned to the bottom of
  // the viewport would have to open its menu upward over itself (§17.5 item 2). Its markup
  // is built fresh each time and dropped on close, so a closed sheet can never leave a
  // second, invisible copy of "Sync now" in the document (§17.6).
  function openPageActionSheet() {
    const items = [
      { act: 'settings', label: 'Edit settings' },
      { act: 'sections', label: 'Customize sections' },
      { sep: true },
      { act: 'force-epg', label: 'Force EPG resync', disabled: A.status === 'SYNCING' },
      { href: A.browseUrl, label: 'Browse channels' },
    ];
    if (A.xtreamDebug && A.accountType === 'xtream') {
      items.push({ sep: true },
        { act: 'dump', label: 'Fetch & dump' },
        { act: 'sync-dump', label: 'Sync from dump' });
    }
    items.push({ sep: true }, { act: 'delete', label: 'Delete account', danger: true });

    const body = document.createElement('div');
    body.className = 'acct-sheet';
    body.innerHTML = items.map((i) => {
      if (i.sep) return '<div class="sheet-sep"></div>';
      if (i.href) return `<a class="sheet-act" href="${escHtml(i.href)}">${escHtml(i.label)}</a>`;
      return `<button class="sheet-act${i.danger ? ' danger' : ''}" data-act="${i.act}"` +
        `${i.disabled ? ' disabled' : ''}>${escHtml(i.label)}</button>`;
    }).join('');
    const sheet = buildModal({ title: A.name, body });
    body.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn || btn.disabled) return;
      sheet.closeModal();
      run(btn.dataset.act);
    });
  }

  function run(act) {
    const handler = act === 'page-actions' ? openPageActionSheet : ACTIONS[act];
    if (!handler) { console.warn('unhandled account action', act); return; }
    handler();
  }

  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-act]');
    // A sheet's own buttons are handled by the sheet, which closes first - letting this
    // delegated handler see them too would run the action twice.
    if (!el || el.disabled || el.closest('.acct-sheet')) return;
    if (el.tagName === 'A') return;
    e.preventDefault();
    closeMenus();
    run(el.dataset.act);
  });

  // ── The sticky bottom bar ────────────────────────────────────────────────
  const bar = $('#acct-stickybar');
  const inlineBar = $('#acct-actionbar');

  function setBar(on) {
    if (!bar) return;
    bar.classList.toggle('on', on);
    bar.setAttribute('aria-hidden', on ? 'false' : 'true');
    // Tracks VISIBLE, not present: this is what lifts toasts clear of the bar, and a short
    // page that never shows it must not shove its toasts up for nothing.
    document.body.classList.toggle('actbar-on', on);
  }

  if (bar) {
    if (!inlineBar || typeof IntersectionObserver === 'undefined') {
      // Both fallbacks resolve toward SHOWING it. A fallback that hid the bar could strand
      // the page's primary action off screen with no way to reach it.
      setBar(true);
    } else {
      const observer = new IntersectionObserver((entries) => {
        entries.forEach((entry) => setBar(!entry.isIntersecting));
      }, { threshold: 0 });
      observer.observe(inlineBar);
      // A live observer toggling a bar that belongs to a page you have left is how a fixed
      // control ends up floating over the wrong page.
      window.addEventListener('pagehide', () => observer.disconnect());
    }
  }
}());
