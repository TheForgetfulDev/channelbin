/* Notifications settings (templates/notifications_settings.html).
   Design: DESIGN.md 15.2/15.3/15.6/15.7, mockups dev/mockups/27-settings-desktop.html +
   28-settings-mobile.html; rollout dev/changelog/440.

   One writer per DOM region, per CLAUDE.md's frontend rules and 15.7 specifically:
   renderServices() is the sole writer for the add bar, the service cards AND the
   none-added empty state, so the three can never describe different sets; the
   heading's count comes from the same computation as the cards it counts.
   renderRouting() is the sole writer for the routing rows, which is why enabling a
   service rebuilds the table from state rather than patching the row that changed. */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const bootEl = $('#notif-boot');
  if (!bootEl) return;
  const BOOT = JSON.parse(bootEl.textContent);

  const SVC = BOOT.services;            // key -> {label, hint, enabled, url, added}
  const ROUTING = BOOT.routing;         // alert type -> {label, severity, in_app, push_services}
  const ALL = Object.keys(SVC);

  const toast = (msg, isError) =>
    showToast(msg, { type: isError ? 'error' : 'success', durationMs: isError ? 6000 : 2500 });

  /* 15.2: added and enabled are two different things. `added` needs no config key -
     the server derives it from "enabled, or a URL is stored". The client can also set
     it optimistically when you add a service, so a card exists to paste a URL into
     before anything has been stored. That state is deliberately not persisted: with
     no URL and not enabled there is nothing in config.yaml to tell it apart from a
     service you never touched, and 15.2 settled that this costs no new key. */
  const addedServices = () => ALL.filter((k) => SVC[k].added);
  const enabledServices = () => ALL.filter((k) => SVC[k].enabled);
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

  // ── Alert routing filter: 21 types and growing needs search + severity/push
  // filtering. Severities match what ALERT_TYPES actually uses (app/alerts.py).
  // All active by default, same as logs.js's level chips - the filter starts as
  // a no-op.
  const SEVERITIES = ['INFO', 'WARN', 'ERROR', 'CRIT'];
  const activeSeverities = new Set(SEVERITIES);
  let pushOnly = false;
  let rtSearchText = '';

  // ── The two dropdowns this page owns (DESIGN.md 15.3) ───────────────────
  registerDropdown('push', {
    title: () => 'Push this alert to',
    rows: () => enabledServices().map((k) => ({ v: k, label: SVC[k].label })),
    on: (key) => ROUTING[key].push_services,
    /* The stored list is never filtered down when a service is switched off - that
       would silently throw away a routing choice on a temporary pause, and
       re-enabling would come back empty. Only the LABEL counts what can currently
       be delivered. Removing a service outright is the one thing that prunes the
       list, because that service is gone (see the DELETE endpoint). */
    label: (key) => ddPickLabel(
      ROUTING[key].push_services.filter((k) => SVC[k] && SVC[k].enabled).map((k) => SVC[k].label),
      'services'),
    toggle: (key, svc, checked) => {
      const list = ROUTING[key].push_services;
      const i = list.indexOf(svc);
      if (checked && i === -1) list.push(svc);
      if (!checked && i !== -1) list.splice(i, 1);
      markRoutingDirty();
      // "Pushes somewhere" is derived from this list, so a toggle can change
      // which rows the filter shows even though the table content is unchanged.
      if (pushOnly) applyRoutingFilter();
    },
  });

  registerDropdown('addsvc', {
    single: true,
    title: () => 'Add a notification service',
    // Only services that are not already added, so the list shrinks as you use it
    // and can never offer a duplicate.
    rows: () => ALL.filter((k) => !SVC[k].added)
      .map((k) => ({ v: k, label: SVC[k].label, sub: SVC[k].hint })),
    label: () => 'Add a service',
    pick: (_, key) => {
      SVC[key].added = true;
      renderServices();
      renderRouting();
      toast(`${SVC[key].label} added. Paste its Apprise URL, then enable it.`);
    },
  });

  // ── Header count ────────────────────────────────────────────────────────
  function syncCount() {
    $('#notif-count').textContent =
      `${enabledServices().length} of ${plural(addedServices().length, 'added service')} enabled`;
  }

  // ── Services region: one writer for the bar, the cards and the empty state ──
  function renderServices() {
    const added = addedServices();
    const left = ALL.length - added.length;
    const bar = $('#svc-bar');
    const grid = $('#svc-grid');

    bar.innerHTML =
      `<div class="svc-head"><h2>Services <span class="cnt">${added.length} of ${ALL.length} added</span></h2></div>` +
      '<div class="svc-bar">' +
      (left
        ? dropdownTriggerHtml('addsvc', 'btn-accent', '+ ')
        : '<button class="btn btn-sm msel" type="button" disabled><span class="mlbl">All services added</span></button>') +
      `<span class="note">A service only gets a card once you add it.${left ? ` ${left} available.` : ''}</span>` +
      '</div>';

    if (!added.length) {
      bar.insertAdjacentHTML('beforeend',
        '<div class="svc-empty">No notification services yet, so nothing is pushed anywhere. ' +
        'Alerts are still raised and still visible in the Alert Center.' +
        '<div><button class="btn btn-primary btn-sm" type="button" data-msel="addsvc">Add a service</button></div></div>');
      grid.style.display = 'none';
      grid.innerHTML = '';
      syncCount();
      return;
    }

    grid.style.display = '';
    grid.innerHTML = added.map((key) => {
      const s = SVC[key];
      return `<section class="card" data-svc="${escHtml(key)}">
        <div class="card-head">
          <h2>${escHtml(s.label)}</h2>
          <div class="card-head-actions">
            <span class="badge ${s.broken ? 'badge-danger' : (s.enabled ? 'badge-success' : 'badge-scheduled')}">${s.broken ? 'Broken' : (s.enabled ? 'Enabled' : 'Disabled')}</span>
            <button class="svc-x" type="button" data-svc-remove="${escHtml(key)}"
              data-tip="Remove ${escHtml(s.label)}&#10;Clears its stored URL and drops it from every routing row. Disabling it instead keeps the URL.">Remove</button>
          </div>
        </div>
        <div class="svc-body">
          ${s.broken ? `<div class="notice notice-warn">The stored URL is still the example placeholder shown below it, not a real credential - pushes to this service are being dropped. Paste the real Apprise URL and save.</div>` : ''}
          <label class="svc-enable">
            <span class="switch"><input type="checkbox" data-svc-toggle="${escHtml(key)}"${s.enabled ? ' checked' : ''}><span class="knob"></span></span>
            <span>Enable ${escHtml(s.label)}</span>
          </label>
          <div>
            <div class="svc-lbl">Apprise URL</div>
            <div class="secret-field">
              <input type="text" value="${escHtml(s.url)}" placeholder="${escHtml(s.hint)}" data-svc-url="${escHtml(key)}">
            </div>
            <div class="svc-hint">${escHtml(s.hint)}</div>
          </div>
          <div>
            <div class="svc-lbl">Rate limit override</div>
            <input type="number" min="0" max="3600" placeholder="Global (${BOOT.rate_limit}s)"
              value="${s.rate_limit_seconds === null || s.rate_limit_seconds === undefined ? '' : s.rate_limit_seconds}"
              data-svc-rate-limit="${escHtml(key)}">
            <div class="svc-hint">Blank uses the global limit. 0 sends immediately, with no batching.</div>
          </div>
          <div class="svc-actions">
            <button class="btn btn-sm" type="button" data-svc-save="${escHtml(key)}">Save</button>
            <button class="btn btn-sm" type="button" data-svc-test="${escHtml(key)}">Send test</button>
          </div>
        </div>
      </section>`;
    }).join('');

    // The eyeball is cloned from the macro's own markup rather than re-typed here,
    // and only where a value is actually stored (the standard in CLAUDE.md
    // §Config secrets: render the reveal only when there is something to reveal).
    const tpl = $('#reveal-tpl');
    if (tpl) {
      added.filter((key) => SVC[key].url).forEach((key) => {
        const field = $(`[data-svc-url="${CSS.escape(key)}"]`, grid).parentElement;
        const btn = tpl.content.firstElementChild.cloneNode(true);
        btn.dataset.secretPath = `notifications.services.${key}.url`;
        field.appendChild(btn);
      });
      initSecretReveal(grid);
    }
    syncCount();
  }

  // ── Alert routing ───────────────────────────────────────────────────────
  function renderRouting() {
    const anyEnabled = enabledServices().length > 0;
    $('#rt-body').innerHTML = Object.entries(ROUTING).map(([key, r]) => {
      const picks = anyEnabled
        ? dropdownTriggerHtml(`push:${key}`)
        : '<span class="rt-none">No services enabled yet</span>';
      const hay = `${r.label} ${key}`.toLowerCase();
      return `<tr data-type="${escHtml(key)}" data-sev="${escHtml(r.severity)}" data-hay="${escHtml(hay)}">
        <td>
          <div class="rt-name">${escHtml(r.label)} <span class="alert-severity sev-${escHtml(r.severity)}">${escHtml(r.severity)}</span></div>
          <div class="rt-key">${escHtml(key)}</div>
        </td>
        <td><span class="rt-cl">Show in the Alert Center</span>
          <label class="switch"><input type="checkbox" data-rt-inapp="${escHtml(key)}"${r.in_app ? ' checked' : ''}><span class="knob"></span></label></td>
        <td class="rt-push"><span class="rt-cl">Push to</span>${picks}</td>
      </tr>`;
    }).join('');
    applyRoutingFilter();
  }

  // ── Alert routing filter bar ───────────────────────────────────────────
  // The visibility half of the one-writer split: content comes from
  // renderRouting() above, this only toggles what's already there. "Pushes
  // somewhere" is derived from live ROUTING state (push selections can change
  // between renders), so it's recomputed here rather than baked into a row's
  // static markup.
  function renderSevChips() {
    $('#rt-sev-chips').innerHTML = SEVERITIES.map((s) =>
      `<button class="chip sev${activeSeverities.has(s) ? ' active' : ''}" type="button"
        data-sev="${s}">${s}</button>`).join('');
  }

  function routingRowVisible(row) {
    if (!activeSeverities.has(row.dataset.sev)) return false;
    if (pushOnly && ROUTING[row.dataset.type].push_services.length === 0) return false;
    if (rtSearchText && !row.dataset.hay.includes(rtSearchText)) return false;
    return true;
  }

  function applyRoutingFilter() {
    const rows = $$('#rt-body tr');
    let shown = 0;
    rows.forEach((row) => {
      const vis = routingRowVisible(row);
      row.hidden = !vis;
      if (vis) shown += 1;
    });
    $('#rt-shown').textContent = shown === rows.length ? '' : `${shown} of ${rows.length} shown`;
    $('#rt-no-results').style.display = rows.length && shown === 0 ? '' : 'none';
    $('#rt-sev-chips').querySelectorAll('.chip').forEach((c) => {
      c.classList.toggle('active', activeSeverities.has(c.dataset.sev));
    });
    $('#rt-push-chip').classList.toggle('active', pushOnly);
  }

  // A permanently-live Save is a dead control: routing saves only once something changed.
  function markRoutingDirty() { $('#rt-save').disabled = false; }

  function saveRouting() {
    const payload = {};
    Object.entries(ROUTING).forEach(([key, r]) => {
      payload[key] = { in_app: r.in_app, push_services: r.push_services };
    });
    jsonFetch('/api/notifications/routing', { method: 'POST', body: JSON.stringify(payload) })
      .then(() => { $('#rt-save').disabled = true; toast('Routing saved'); })
      .catch((e) => toast(e.message || 'Could not save routing', true));
  }

  // ── One service ─────────────────────────────────────────────────────────
  function saveService(key, { quiet = false } = {}) {
    const input = $(`[data-svc-url="${CSS.escape(key)}"]`);
    const url = input ? input.value.trim() : SVC[key].url;
    SVC[key].url = url;
    const rlInput = $(`[data-svc-rate-limit="${CSS.escape(key)}"]`);
    const rlRaw = rlInput ? rlInput.value.trim() : '';
    const rateLimitSeconds = rlRaw === '' ? null : Number(rlRaw);
    SVC[key].rate_limit_seconds = rateLimitSeconds;
    return jsonFetch(`/api/notifications/services/${encodeURIComponent(key)}`, {
      method: 'POST',
      body: JSON.stringify({ enabled: SVC[key].enabled, url, rate_limit_seconds: rateLimitSeconds }),
    }).then(() => { if (!quiet) toast(`${SVC[key].label} saved`); })
      .catch((e) => { toast(e.message || 'Could not save the service', true); throw e; });
  }

  function testService(key) {
    // Save first so the test uses the URL currently in the box, not the stored one.
    saveService(key, { quiet: true })
      .then(() => jsonFetch(`/api/notifications/services/${encodeURIComponent(key)}/test`, { method: 'POST' }))
      .then(() => toast(`Test sent through ${SVC[key].label}`))
      .catch((e) => { if (e && e.message) toast(e.message, true); });
  }

  function removeService(key) {
    buildModal({
      title: `Remove ${SVC[key].label}?`,
      body: `<p>Its stored URL is cleared and any alerts routed to it stop being pushed. ` +
            `You can add it again later, but you will have to paste the URL again.</p>` +
            `<p class="fl-desc">Disabling it instead keeps the URL.</p>`,
      footer: [
        { label: 'Keep it', class: 'btn' },
        {
          label: 'Remove service',
          class: 'btn btn-danger',
          // buildModal only auto-closes footer buttons that have NO onClick, so this
          // one closes itself before the request goes out.
          onClick: (close) => {
            close();
            jsonFetch(`/api/notifications/services/${encodeURIComponent(key)}`, { method: 'DELETE' })
              .then(() => {
                SVC[key] = { ...SVC[key], enabled: false, url: '', added: false };
                // The server prunes the stored routing in the same save; mirror it here
                // so the table and config.yaml cannot disagree about what is routed.
                Object.values(ROUTING).forEach((r) => {
                  r.push_services = r.push_services.filter((s) => s !== key);
                });
                renderServices();
                renderRouting();
                toast(`${SVC[key].label} removed`);
              })
              .catch((e) => toast(e.message || 'Could not remove the service', true));
          },
        },
      ],
    });
  }

  // ── Delivery ────────────────────────────────────────────────────────────
  function updateUrlPreview() {
    const val = $('#base-url-input').value.trim();
    $('#base-url-preview').textContent = val
      ? `Example link: ${val.replace(/\/+$/, '')}/recordings/44`
      : 'No link will be included.';
  }

  function saveBaseUrl() {
    jsonFetch('/api/settings/field', {
      method: 'POST',
      body: JSON.stringify({ path: 'notifications.base_url', value: $('#base-url-input').value.trim() }),
    }).then(() => toast('Saved')).catch((e) => toast(e.message || 'Could not save', true));
  }

  function saveRateLimit() {
    const val = parseInt($('#rate-limit-input').value, 10);
    if (isNaN(val) || val < 1) { toast('Push rate limit must be at least 1 second', true); return; }
    jsonFetch('/api/notifications/rate-limit', { method: 'POST', body: JSON.stringify({ seconds: val }) })
      .then(() => toast('Saved')).catch((e) => toast(e.message || 'Could not save', true));
  }

  // ── Wiring ──────────────────────────────────────────────────────────────
  // A URL typed into a card saves on a pause rather than per keystroke; the explicit
  // Save button stays, because a debounce is invisible and a credential field wants a
  // confirmation you can point at.
  let urlTimer = null;

  document.addEventListener('click', (e) => {
    const remove = e.target.closest('[data-svc-remove]');
    if (remove) { removeService(remove.dataset.svcRemove); return; }
    const save = e.target.closest('[data-svc-save]');
    if (save) { saveService(save.dataset.svcSave); return; }
    const test = e.target.closest('[data-svc-test]');
    if (test) { testService(test.dataset.svcTest); return; }
    if (e.target.closest('#rt-save')) { saveRouting(); return; }
    const sevChip = e.target.closest('#rt-sev-chips .chip');
    if (sevChip) {
      const s = sevChip.dataset.sev;
      if (activeSeverities.has(s)) activeSeverities.delete(s); else activeSeverities.add(s);
      applyRoutingFilter();
      return;
    }
    if (e.target.closest('#rt-push-chip')) { pushOnly = !pushOnly; applyRoutingFilter(); return; }
    if (e.target.closest('#rt-search-clear')) {
      $('#rt-search').value = '';
      rtSearchText = '';
      $('#rt-search-wrap').classList.remove('has-text');
      applyRoutingFilter();
      $('#rt-search').focus();
    }
  });

  document.addEventListener('change', (e) => {
    const toggle = e.target.closest('[data-svc-toggle]');
    if (toggle) {
      const key = toggle.dataset.svcToggle;
      SVC[key].enabled = toggle.checked;
      saveService(key, { quiet: true })
        .then(() => toast(`${SVC[key].label} ${toggle.checked ? 'enabled' : 'disabled'}`))
        .catch(() => { SVC[key].enabled = !toggle.checked; renderServices(); renderRouting(); });
      // Enabling a service changes what every routing row can offer, so the table is
      // rebuilt from state rather than patched - the two cannot disagree about which
      // services exist. Same reason the cards are rebuilt: the badge is derived.
      renderServices();
      renderRouting();
      return;
    }
    const inApp = e.target.closest('[data-rt-inapp]');
    if (inApp) { ROUTING[inApp.dataset.rtInapp].in_app = inApp.checked; markRoutingDirty(); return; }
    if (e.target.id === 'rate-limit-input') saveRateLimit();
  });

  document.addEventListener('input', (e) => {
    const url = e.target.closest('[data-svc-url]');
    if (url) {
      clearTimeout(urlTimer);
      const key = url.dataset.svcUrl;
      urlTimer = setTimeout(() => saveService(key, { quiet: true }), 1200);
      return;
    }
    if (e.target.id === 'base-url-input') updateUrlPreview();
    if (e.target.id === 'rt-search') {
      const value = e.target.value;
      rtSearchText = value.toLowerCase().trim();
      $('#rt-search-wrap').classList.toggle('has-text', !!value);
      applyRoutingFilter();
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && e.target.id === 'rt-search' && e.target.value) {
      $('#rt-search-clear').click();
    }
  });

  $('#base-url-input').addEventListener('blur', saveBaseUrl);

  updateUrlPreview();
  renderServices();
  renderSevChips();
  renderRouting();
})();
