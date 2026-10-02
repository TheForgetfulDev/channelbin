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

   4. **The page keeps itself current through a sync** (dev/changelog/1152), the way the
      Accounts list does (dev/changelog/921): driven by base.html's /api/nav-status poll, it
      swaps LIVE_REGIONS for a fresh server render when the sync signature moves or the
      render is a minute old. The template stays the only thing that knows how those regions
      look, so every handler here is delegated on document. What a sync changes as DATA -
      the status, the counts the confirms quote, the first page of history - comes from the
      swapped #acct-state blob, never from the load-time ACCOUNT_DETAIL, which keeps only
      what a sync cannot move.

   Depends on util.js (escHtml, fmtDur, jsonFetch, showToast, buildModal, closeMenus),
   account-actions.js (confirmAccountSync, accountCancelSync, confirmForceEpgResync,
   confirmDeleteAccount, confirmBlockAccount, accountUnblock - shared with the list page) and account-modal.js (openAccountModal).
*/
(function () {
  const A = window.ACCOUNT_DETAIL;
  if (!A) return;

  const $ = (sel, root = document) => root.querySelector(sel);

  function readState() {
    const el = $('#acct-state');
    if (!el) return {};
    try {
      return JSON.parse(el.textContent);
    } catch (err) {
      console.warn('The account page state blob did not parse; keeping the last one.', err);
      return {};
    }
  }
  Object.assign(A, readState());

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
    // b-running, not b-live: a sync in progress is not an alarm (dev/changelog/816). A
    // running sync's row says IN_PROGRESS (app/accounts.py opens it that way); a NULL is
    // read the same.
    if (!status || status === 'IN_PROGRESS') return '<span class="badge b-running"><span class="pulse"></span>Running</span>';
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

  const loadAllSyncs = () => jsonFetch(`/api/accounts/${A.id}/syncs`).then((res) => {
    logs = res.logs || [];
    renderHistory();
  });

  // "All N syncs" loads the rest into this same section and becomes "Show fewer" -
  // account_logs.html and its route are retired (§17.1), so there is no page to go to.
  // Delegated: the button lives in #acct-hist-head, which a refresh replaces.
  document.addEventListener('click', (e) => {
    const moreBtn = e.target.closest('#acct-hist-more');
    if (!moreBtn || moreBtn.disabled) return;
    if (expanded) {
      logs = (A.logs || []).slice();
      expanded = false;
      moreBtn.textContent = `All ${fmtNum(Number(moreBtn.dataset.total))} syncs`;
      renderHistory();
      return;
    }
    moreBtn.disabled = true;
    loadAllSyncs()
      .then(() => {
        expanded = true;
        // Looked up again: a refresh may have replaced the button while the fetch ran.
        const btn = $('#acct-hist-more');
        if (btn) btn.textContent = 'Show fewer';
      })
      .catch((err) => showToast(err.message || 'Could not load the sync history.', { type: 'error' }))
      .finally(() => { moreBtn.disabled = false; });
  });

  renderHistory();

  // ── Section layout (order + hidden), persisted server-side ───────────────
  const SEC_NAMES = { details: 'Details', hosts: 'Hosts', logins: 'Logins', content: 'Content', sources: 'EPG sources',
                      usage: 'Usage', history: 'Sync history', activity: 'Activity' };
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
  // An action that only moves what a sync moves refreshes the page in place rather than
  // reloading it, so the history expansion and the scroll position survive it. `refresh`
  // is hoisted from the live-refresh section below.
  const hooks = () => {
    showActionError('');
    return { onDone: () => refresh(), onError: showActionError };
  };

  // ── Hosts (DESIGN-account-providers.md §4) ───────────────────────────────
  // The list is the user's and only the three explicit actions here move it. Each one
  // refreshes the page in place afterward, like a sync action, so the card shows the
  // server's view of the list rather than a copy this file would have to keep right.
  const hostUrl = (id) => `/api/accounts/${A.id}/hosts${id ? `/${id}` : ''}`;

  function openAddHost() {
    const body = document.createElement('div');
    body.innerHTML =
      fieldRow({
        label: 'Host', stack: true,
        meta: 'A host name, with its port if the stream URLs carry one - like ' +
          '<code>a2.skyline.example</code> or <code>a2.skyline.example:8080</code>. A pasted ' +
          'URL is reduced to its host.',
        control: '<input type="text" id="host-name" autocomplete="off" spellcheck="false">',
      }) +
      '<p class="text-muted small" style="margin-top:8px">Only list names your reseller gave ' +
      'you for this same account. The first one you add also lists the host this account\'s ' +
      'stream URLs use today, as the active one, so the list starts from what is true now.</p>';
    const input = body.querySelector('#host-name');
    const modal = buildModal({
      title: 'Add host',
      body,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (c) => c() },
        {
          label: 'Add host',
          class: 'btn btn-primary',
          onClick: (close) => {
            const host = input.value.trim();
            if (!host) { showToast('Enter a host name.', { type: 'error' }); return false; }
            post(hostUrl(), { method: 'POST', body: JSON.stringify({ host }) })
              .then(() => { close(); refresh(); })
              .catch(() => {});
            return false;
          },
        },
      ],
    });
    setTimeout(() => input.focus(), 50);
    return modal;
  }

  function confirmActivateHost(el) {
    const host = el.dataset.host || '';
    confirmModal({
      title: 'Make this host active',
      message: `Move every stream URL on this account to ${host}?`,
      consequence: 'A running recording picks the new host up at its next segment.',
      confirmLabel: 'Make active',
    }).then((ok) => {
      if (!ok) return;
      post(`${hostUrl(el.dataset.hostId)}/activate`, { method: 'POST' })
        .then(() => refresh()).catch(() => {});
    });
  }

  function confirmRemoveHost(el) {
    const host = el.dataset.host || '';
    const isActive = el.dataset.active === '1';
    const others = Number(el.dataset.others || 0);
    if (isActive && others > 0) {
      showToast(`${host} is the active host. Make another host active first.`, { type: 'error' });
      return;
    }
    confirmModal({
      title: 'Remove host',
      message: `Remove ${host} from this account's host list?`,
      consequence: isActive
        ? 'It is the only host listed, so the list empties and the stream URLs stay as they are.'
        : 'Stream URLs are not changed.',
      confirmLabel: 'Remove',
      danger: true,
    }).then((ok) => {
      if (!ok) return;
      post(hostUrl(el.dataset.hostId), { method: 'DELETE' })
        .then(() => refresh()).catch(() => {});
    });
  }

  // ── Logins (DESIGN-account-providers.md §5, §6.3) ────────────────────────
  // Add and Edit share one dialog. The password is never served back: the edit form shows
  // a blank field that means "keep", and the eye reveals only what is being typed, exactly
  // as the account's own password works (DESIGN.md §17.4).
  const loginUrl = (id) => `/api/accounts/${A.id}/logins${id ? `/${id}` : ''}`;

  function openLoginDialog(existing, seatPrefill, sharedWith) {
    const edit = !!existing;
    const body = document.createElement('div');
    body.innerHTML =
      (sharedWith
        ? `<p class="card-note">This login is shared with ${escHtml(sharedWith)}. A change ` +
          'here applies to every account holding it.</p>'
        : '') +
      fieldRow({
        label: 'Name', stack: true,
        meta: 'What this login is called here, like <code>main</code> or <code>spare</code>.',
        control: '<input type="text" id="login-name" autocomplete="off">',
      }) +
      fieldRow({
        label: 'Username', stack: true,
        control: '<input type="text" id="login-user" autocomplete="off" spellcheck="false">',
      }) +
      fieldRow({
        label: 'Password', stack: true,
        meta: edit
          ? 'Leave blank to keep the existing password. ChannelBin never shows a stored ' +
            'password back to you - the eye only reveals what you type here.'
          : 'The eye only reveals what you type here - ChannelBin never shows a stored ' +
            'password back to you.',
        control: '<span class="secret-field">' +
          '<input type="password" id="login-pass" autocomplete="new-password" ' +
          `placeholder="${edit ? 'Leave blank to keep existing' : 'Password'}">` +
          '<button type="button" class="secret-reveal-btn" ' +
          'aria-label="Show what I am typing" title="Show what I am typing">' +
          ACCOUNT_EYE_SVG + '</button></span>',
      }) +
      fieldRow({
        label: 'Seats', stack: true,
        meta: 'How many streams this login may have open at once. Pre-filled from what the ' +
          'provider reports for the account, or the account\'s own limit.',
        control: '<input type="number" id="login-seats" min="1" step="1">',
      });
    const $f = (id) => body.querySelector(`#${id}`);
    if (edit) {
      $f('login-name').value = existing.name || '';
      $f('login-user').value = existing.username || '';
      $f('login-seats').value = existing.max_connections || 1;
    } else {
      $f('login-seats').value = seatPrefill || 1;
    }
    initLocalReveal(body);
    const modal = buildModal({
      title: edit ? 'Edit login' : 'Add login',
      body,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (c) => c() },
        {
          label: edit ? 'Save' : 'Add login',
          class: 'btn btn-primary',
          onClick: (close) => {
            const payload = {
              name: $f('login-name').value.trim(),
              username: $f('login-user').value.trim(),
              password: $f('login-pass').value,
              max_connections: $f('login-seats').value,
            };
            if (!payload.name) { showToast('Give the login a name.', { type: 'error' }); return false; }
            if (!payload.username) { showToast('Enter the username.', { type: 'error' }); return false; }
            if (!edit && !payload.password) { showToast('Enter the password.', { type: 'error' }); return false; }
            post(loginUrl(edit ? existing.id : null), { method: 'POST', body: JSON.stringify(payload) })
              .then(() => { close(); refresh(); })
              .catch(() => {});
            return false;
          },
        },
      ],
    });
    setTimeout(() => $f('login-name').focus(), 50);
    return modal;
  }

  function openAddLogin() {
    jsonFetch(loginUrl())
      .then((res) => openLoginDialog(null, res.seat_prefill))
      .catch((e) => showToast(e.message || 'Request failed.', { type: 'error' }));
  }

  function openEditLogin(el) {
    jsonFetch(loginUrl())
      .then((res) => {
        const login = (res.logins || []).find((l) => String(l.id) === el.dataset.loginId);
        if (!login) { showToast('Login not found.', { type: 'error' }); return; }
        openLoginDialog(login, null, el.dataset.sharedWith || '');
      })
      .catch((e) => showToast(e.message || 'Request failed.', { type: 'error' }));
  }

  function moveLogin(el) {
    post(`${loginUrl(el.dataset.loginId)}/move`,
      { method: 'POST', body: JSON.stringify({ direction: el.dataset.direction }) })
      .then(() => refresh()).catch(() => {});
  }

  function confirmRemoveLogin(el) {
    const name = el.dataset.login || '';
    const held = Number(el.dataset.held || 0);
    const sharedWith = el.dataset.sharedWith || '';
    confirmModal({
      title: 'Remove login',
      message: `Remove login "${name}" from this account's list?`,
      consequence: sharedWith
        ? `${sharedWith} keeps it, with its seats. This account's connection limit drops by its seats.`
        : held > 0
        ? 'A recording, health check or preview seated on it keeps its seat until it ends; nothing is cancelled. The account\'s connection limit drops by its seats.'
        : 'The account\'s connection limit drops by its seats.',
      confirmLabel: 'Remove',
      danger: true,
    }).then((ok) => {
      if (!ok) return;
      post(loginUrl(el.dataset.loginId), { method: 'DELETE' })
        .then(() => refresh()).catch(() => {});
    });
  }

  // ── EPG sources (DESIGN-epg-sources.md §9.2) ────────────────────────────
  // Add and Edit share one dialog. A new or re-pointed source is fetched straight away by
  // the server, which says so in its message; the page reloads to show the new row.
  const sourceUrl = (id) => `/api/accounts/${A.id}/epg-sources${id ? `/${id}` : ''}`;

  function openSourceModal(src) {
    const editing = !!src;
    const s = src || { name: '', url: '', refresh_interval_hours: '', enabled: true };
    const hours = (A.sourceHours || []).map((h) =>
      `<option value="${h}"${String(s.refresh_interval_hours) === String(h) ? ' selected' : ''}>` +
      `Every ${h} hour${h === 1 ? '' : 's'}</option>`).join('');
    const body = document.createElement('div');
    body.innerHTML =
      fieldRow({
        label: 'XMLTV URL', stack: true,
        meta: 'A TV guide file, plain or <code>.gz</code>. Kept secret like the account\'s ' +
          'own URLs.' + (editing ? ' Changing it fetches the new file straight away.' : ''),
        control: `<input type="url" id="src-url" value="${escHtml(s.url)}" ` +
          'placeholder="http://example.com/guide.xml.gz">',
      }) +
      fieldRow({
        label: 'Name',
        meta: 'Shown wherever this source is named. Blank uses the account name.',
        control: `<input type="text" id="src-name" value="${escHtml(s.name)}">`,
        wide: true,
      }) +
      fieldRow({
        label: 'Refresh',
        meta: 'How often the file is fetched again. With the account\'s sync, Sync now ' +
          'fetches it too.',
        control: '<select id="src-hours">' +
          `<option value=""${s.refresh_interval_hours ? '' : ' selected'}>With this account's sync</option>` +
          `${hours}</select>`,
      }) +
      (editing ? fieldRow({
        label: 'On',
        meta: 'Turned off, the source keeps its listings and is not fetched again.',
        control: '<label class="switch"><input type="checkbox" id="src-on"' +
          `${s.enabled ? ' checked' : ''}><span class="knob"></span></label>`,
      }) : '');
    const $m = (sel) => body.querySelector(sel);
    buildModal({
      title: editing ? 'Edit EPG source' : 'Add EPG source',
      body,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (c) => c() },
        {
          label: editing ? 'Save' : 'Add source',
          class: 'btn btn-primary',
          onClick: (close) => {
            const url = $m('#src-url').value.trim();
            if (!url) { showToast('An XMLTV URL is required.', { type: 'error' }); return false; }
            const payload = {
              url,
              name: $m('#src-name').value,
              refresh_interval_hours: $m('#src-hours').value,
              enabled: editing ? $m('#src-on').checked : true,
            };
            jsonFetch(sourceUrl(editing ? s.id : null), {
              method: 'POST', body: JSON.stringify(payload),
            }).then((res) => {
              close();
              showToast(res.message || 'Saved.', { durationMs: 7000 });
              setTimeout(reload, 1500);
            }).catch((e) => showToast(e.message || 'Could not save the source.', { type: 'error' }));
            return false;
          },
        },
      ],
    });
  }

  function editSource(el) {
    jsonFetch(sourceUrl(el.dataset.sourceId))
      .then((res) => openSourceModal(res.source))
      .catch((e) => showToast(e.message || 'Could not load the source.', { type: 'error' }));
  }

  // "Another account's guide" (DESIGN-epg-sources.md §3): a subscription to a source some
  // other account owns and refreshes. Each option says what it would do here today, which
  // is what makes the choice between two sources a decision rather than a guess.
  const fmtN = (v) => Number(v).toLocaleString('en-US');

  function borrowNote(s) {
    if (s.covers === null) {
      return 'No successful refresh yet, so which of this account\'s channels it covers is not known.';
    }
    if (!s.covers) return 'Its last refresh has no listings for any channel on this account.';
    const out = [`Covers ${fmtN(s.covers)} of this account's channels.`];
    if (s.fills) out.push(`${fmtN(s.fills)} with no guide now would get one.`);
    if (s.switches) out.push(`${fmtN(s.switches)} would switch to it from a source listing one program all day.`);
    if (!s.fills && !s.switches) out.push('Every one of them keeps the guide it has now.');
    return out.join(' ');
  }

  function openAddSource() {
    jsonFetch(sourceUrl('borrowable'))
      .then((res) => {
        if (!(res.sources || []).length) { openSourceModal(null); return; }
        openBorrowModal(res.sources);
      })
      .catch(() => openSourceModal(null));
  }

  function openBorrowModal(sources) {
    const body = document.createElement('div');
    const opt = (value, title, sub) =>
      `<label class="fp-radio"><input type="radio" name="src-pick" value="${value}">` +
      `<strong>${title}</strong><span class="text-muted small">${sub}</span></label>`;
    body.innerHTML =
      '<p class="text-muted small">Another account\'s guide goes last in this account\'s ' +
      'order, so it only gives a guide to channels nothing above it covers. Nothing is ' +
      'downloaded twice: its account keeps refreshing it.</p>' +
      '<div style="margin-top:8px">' +
      opt('url', 'An XMLTV URL', 'A guide file of your own, fetched right away.') +
      sources.map((s) => opt(String(s.id), escHtml(s.name),
        `${escHtml(s.owner_name || '')} - ${escHtml(s.kind)}` +
        `${s.last_success_at ? `, refreshed ${escHtml(s.last_success_at)}` : ''}` +
        `${s.is_url && !s.enabled ? ', turned off on its account' : ''}<br>${borrowNote(s)}`)).join('') +
      '</div>';
    buildModal({
      title: 'Add EPG source',
      body,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (c) => c() },
        {
          label: 'Add source',
          class: 'btn btn-primary',
          onClick: (close) => {
            const picked = body.querySelector('input[name="src-pick"]:checked');
            if (!picked) { showToast('Choose a source.', { type: 'error' }); return false; }
            if (picked.value === 'url') { close(); openSourceModal(null); return false; }
            // Copying a large feed's listings takes a few seconds (dev/changelog/1106).
            showToast('Adding the source and copying the listings it already has...',
              { durationMs: 15000 });
            jsonFetch(sourceUrl('subscribe'), {
              method: 'POST', body: JSON.stringify({ source_id: Number(picked.value) }),
            }).then((res) => {
              close();
              showToast(res.message || 'Added.', { durationMs: 10000 });
              setTimeout(reload, 1500);
            }).catch((e) => showToast(e.message || 'Could not add the source.', { type: 'error' }));
            return false;
          },
        },
      ],
    });
  }

  // Sentence one says what happens; sentence two what it costs, counted from the row.
  function confirmSourceRemoval(el, { title, verb, url, what }) {
    const n = Number(el.dataset.activeHere || 0);
    let cost = n
      ? `${n.toLocaleString('en-US')} channel${n === 1 ? ' on this account gets its' : 's on this account get their'} ` +
        'guide from it now. Each switches to the next source that covers it, or has no guide.'
      : 'No channel on this account gets its guide from it right now.';
    if (el.dataset.readers && verb === 'Delete source') {
      cost += ` Other accounts read it too, and lose it: ${escHtml(el.dataset.readers)}. ` +
        'Each of those channels switches to its next source, or has no guide.';
    }
    buildModal({
      title,
      body: `<p>${what(escHtml(el.dataset.sourceName || ''), el.dataset)}</p>` +
        `<p class="text-muted small" style="margin-top:8px">${cost}</p>`,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (c) => c() },
        {
          label: verb,
          class: 'btn btn-danger',
          onClick: (close) => {
            post(url, { method: url.endsWith('stop-using') ? 'POST' : 'DELETE' })
              .then(() => { close(); setTimeout(reload, 1500); })
              .catch(() => close());
            return false;
          },
        },
      ],
    });
  }

  const ACTIONS = {
    'source-add': openAddSource,
    'source-edit': editSource,
    'source-refresh': (el) => post(`${sourceUrl(el.dataset.sourceId)}/refresh`, { method: 'POST' })
      .then(() => setTimeout(reload, 1500)),
    'source-delete': (el) => confirmSourceRemoval(el, {
      title: 'Delete EPG source', verb: 'Delete source', url: sourceUrl(el.dataset.sourceId),
      what: (name) => `Delete <strong>${name}</strong> and every listing it imported?`,
    }),
    'source-stop': (el) => confirmSourceRemoval(el, {
      title: 'Stop using this guide', verb: 'Stop using',
      url: `${sourceUrl(el.dataset.sourceId)}/stop-using`,
      what: (name, d) => {
        const head = `Stop using <strong>${name}</strong> on this account? Its listings are removed`;
        if (d.ownerName) {
          return `${head} from this account's channels. ${escHtml(d.ownerName)} keeps it, and ` +
            'Add source brings it back.';
        }
        if (d.readers) {
          return `${head}. It is still downloaded for the other accounts reading it: ` +
            `${escHtml(d.readers)}. ` +
            'Use again brings it back at the next sync.';
        }
        return `${head} and it is no longer downloaded. Use again brings it back at the next sync.`;
      },
    }),
    'source-use': (el) => post(`${sourceUrl(el.dataset.sourceId)}/use-again`, { method: 'POST' })
      .then(() => setTimeout(reload, 1500)),
    // Re-deciding every channel's guide can take a few seconds on a large account, so the
    // toast says the move is under way rather than leaving the menu looking dead.
    'source-move': (el) => {
      showToast('Moving the source and re-deciding each channel\'s guide...');
      return post(`${sourceUrl(el.dataset.sourceId)}/move`,
        { method: 'POST', body: JSON.stringify({ direction: el.dataset.direction }) })
        .then(() => setTimeout(reload, 1500));
    },
    'host-add': openAddHost,
    'host-activate': confirmActivateHost,
    'host-remove': confirmRemoveHost,
    'login-add': openAddLogin,
    'login-edit': openEditLogin,
    'login-move': moveLogin,
    'login-remove': confirmRemoveLogin,
    sync: () => confirmAccountSync(A.id, A.name, A.syncPrompt, hooks()),
    'cancel-sync': () => accountCancelSync(A.id, hooks()),
    'force-epg': () => confirmForceEpgResync(A.id, hooks()),
    settings: () => openAccountModal({ accountId: A.id, onDone: reload }),
    block: () => confirmBlockAccount(Object.assign(hooks(), {
      id: A.id, name: A.name, limit: A.maxConnections, maxHours: A.blockMaxHours,
    })),
    unblock: (el) => accountUnblock(A.id, el.dataset.blockId, hooks()),
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
      { act: 'block', label: 'Block account use' },
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

  function run(act, el) {
    const handler = act === 'page-actions' ? openPageActionSheet : ACTIONS[act];
    if (!handler) { console.warn('unhandled account action', act); return; }
    handler(el);
  }

  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-act]');
    // A sheet's own buttons are handled by the sheet, which closes first - letting this
    // delegated handler see them too would run the action twice.
    if (!el || el.disabled || el.closest('.acct-sheet')) return;
    if (el.tagName === 'A') return;
    e.preventDefault();
    closeMenus();
    run(el.dataset.act, el);
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

  let observer = null;
  if (bar) {
    if (!inlineBar || typeof IntersectionObserver === 'undefined') {
      // Both fallbacks resolve toward SHOWING it. A fallback that hid the bar could strand
      // the page's primary action off screen with no way to reach it.
      setBar(true);
    } else {
      observer = new IntersectionObserver((entries) => {
        entries.forEach((entry) => setBar(!entry.isIntersecting));
      }, { threshold: 0 });
      observer.observe(inlineBar);
      // A live observer toggling a bar that belongs to a page you have left is how a fixed
      // control ends up floating over the wrong page.
      window.addEventListener('pagehide', () => observer.disconnect());
    }
  }

  // ── Live refresh (dev/changelog/1152) ────────────────────────────────────
  // Two triggers, as on the Accounts list, and each covers what the other cannot:
  //   1. the signature differs from the one this page was rendered at - some sync started,
  //      finished, failed or was cancelled since. This is what stops "Syncing now" and a
  //      Sync now button outliving the sync they describe;
  //   2. the render is a minute old, so "Synced 4m ago" and "Next sync in 23h 44m" do not
  //      quietly drift. Skipped in a background tab, where nobody reads them.
  // The sticky bar swaps only its contents: the bar itself carries whether it is showing,
  // and replacing it would drop that and animate it back in. The history body is swapped
  // only while it holds the "No syncs yet" state - once #acct-hist exists, renderHistory()
  // is its one writer. The Usage card is left out: a sync moves none of its numbers.
  const LIVE_REGIONS = ['#acct-state', '#acct-head', '#acct-notices', '#acct-actionbar',
    '#acct-stickybar-in', '#acct-details-body', '#acct-hosts-head', '#acct-hosts-body',
    '#acct-logins-head', '#acct-logins-body', '#acct-content-body',
    '#acct-sources-body', '#acct-hist-head', '#acct-activity-body'];
  const RERENDER_MS = 60 * 1000;
  let renderedAt = Date.now();
  let inFlight = false;
  let again = false;

  // Replacing a region under an open kebab or the phone's action sheet would close it
  // mid-choice; the next poll simply asks again.
  const busy = () => Boolean(document.querySelector('.menu.open, .acct-sheet'));

  function reapply() {
    // The inline bar is a new element now, and an observer watching the old, detached one
    // would report it gone and pin the sticky bar on.
    if (observer) {
      observer.disconnect();
      const fresh = $('#acct-actionbar');
      if (fresh) observer.observe(fresh);
    }
    const btn = $('#acct-hist-more');
    if (btn && expanded) btn.textContent = 'Show fewer';
  }

  function refresh() {
    // One at a time. A request made while one is running (an action's own refresh landing
    // on a poll's) runs once it finishes, so the page ends on the later state.
    if (inFlight) { again = true; return Promise.resolve(); }
    inFlight = true;
    const regions = $('#acct-hist') ? LIVE_REGIONS : LIVE_REGIONS.concat('#acct-hist-body');
    const shownLogs = JSON.stringify(A.logs || []);
    return swapFromServer(regions)
      .then(() => {
        renderedAt = Date.now();
        Object.assign(A, readState());
        reapply();
        const moved = JSON.stringify(A.logs || []) !== shownLogs;
        // Redrawn only when the runs changed, so a minute tick leaves a hovered row alone.
        if (!moved && $('#acct-hist') && $('#acct-hist').childElementCount) return null;
        if (expanded) return loadAllSyncs();
        logs = (A.logs || []).slice();
        renderHistory();
        return null;
      })
      .catch((err) => console.warn('Account page refresh failed; the next poll retries.', err))
      .finally(() => {
        inFlight = false;
        if (again) { again = false; refresh(); }
      });
  }

  window.__applyAccountSync = (sig) => {
    if (typeof sig !== 'string' || inFlight || busy()) return;
    const changed = sig !== A.syncSig;
    if (!changed && (Date.now() - renderedAt < RERENDER_MS || document.hidden)) return;
    refresh();
  };
}());
