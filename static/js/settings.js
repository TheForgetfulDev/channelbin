/* Settings page (templates/settings.html).
   Design: DESIGN.md 15, mockups dev/mockups/27-settings-desktop.html +
   28-settings-mobile.html; rollout dev/changelog/439.

   One updater per DOM region, per CLAUDE.md's frontend rules: applyFilters() is
   the sole writer for the field rows, the section counts, the rail, the mobile
   picker bar, the open section sheet, the group headings, the "more in Advanced"
   footers, the Basic/Advanced switch, the surface results, the hit chip, the
   cross-tier notice, the changed-from-default chip, the gated-row dim and the empty
   state. Its inputs are the search query, the Basic/Advanced view and the
   changed-from-default filter (DESIGN.md 15.9), so a row can never be hidden by one of
   them and counted by another; the live values of the gating controls decide the dim. */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const bootEl = $('#settings-boot');
  if (!bootEl) return;
  const BOOT = JSON.parse(bootEl.textContent);

  // The sections are read off the DOM rather than restated here. One list, so the
  // rail, the picker sheet and the cards cannot disagree about what exists.
  const SECTIONS = $$('.sec-card').map((el) => ({
    id: el.dataset.sec,
    title: el.dataset.secTitle,
    el,
    total: $$('.frow[data-path]', el).length,
    basic: $$('.frow[data-path]:not([data-tier="advanced"])', el).length,
  }));
  // Every field, whatever the view: search reaches all of them, so the search box and
  // the hit chip count all of them.
  const FIELD_TOTAL = SECTIONS.reduce((n, s) => n + s.total, 0);
  const secById = (id) => SECTIONS.find((s) => s.id === id);

  const page = $('.set-page');
  // The server stamped the saved view; this is the only place it is read back.
  const state = {
    cur: SECTIONS.length ? SECTIONS[0].id : '',
    q: '',
    view: page.dataset.view === 'advanced' ? 'advanced' : 'basic',
    changed: false,
    hits: {},
    empty: {},
  };

  // An element with no tier (the LAN-exposure warning) is never filtered by view.
  const inView = (el) => state.view === 'advanced' || el.dataset.tier !== 'advanced';
  const viewTotal = (sec) => (state.view === 'advanced' ? sec.total : sec.basic);

  const toast = (msg, isError) =>
    showToast(msg, { type: isError ? 'error' : 'success', durationMs: 2500 });

  // `many` is for words that do not take a bare s ("match" -> "matches").
  const plural = (n, word, many = `${word}s`) => `${n} ${n === 1 ? word : many}`;

  // A section's count while a filter is on, the one wording the head, the picker bar and
  // the sheet all use. Null when nothing filters, where each shows its own plain count.
  const filterCount = (n) => {
    if (state.q.trim()) return n ? plural(n, 'match', 'matches') : 'no matches';
    if (state.changed) return n ? `${n} changed` : 'none changed';
    return null;
  };
  const isFiltering = () => !!state.q.trim() || state.changed;

  // ── Tabs ────────────────────────────────────────────────────────────────
  $$('.tab[data-tab]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      $('#pane-gui').style.display = tab === 'gui' ? '' : 'none';
      $('#pane-yaml').style.display = tab === 'yaml' ? '' : 'none';
      $$('.tab[data-tab]').forEach((b) => {
        const on = b === btn;
        b.classList.toggle('active', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
      });
    });
  });

  // ── config.yaml tab: Validate ───────────────────────────────────────────
  // renderYamlCheck is the only writer of #yaml-check after first paint; the server
  // renders the same markup when it refuses a save (templates/settings.html).
  const yamlCheck = $('#yaml-check');
  const yamlText = $('#pane-yaml textarea');
  const renderYamlCheck = (problems) => {
    const errors = problems.filter((p) => p.severity === 'error').length;
    const n = errors || problems.length;
    const badge = problems.length
      ? `<span class="badge ${errors ? 'b-fail' : 'b-warn'}">${n} ${errors ? 'problem' : 'warning'}${n === 1 ? '' : 's'}</span>`
      : '<span class="badge b-done">Valid</span>';
    const items = problems.map((p) => `<li class="yaml-${escHtml(p.severity)}">`
      + (p.line ? `<span class="yaml-where">Line ${p.line}</span>` : '')
      + (p.path ? `<code>${escHtml(p.path)}</code>` : '')
      + ` ${escHtml(p.message)}</li>`).join('');
    yamlCheck.innerHTML = badge + (items ? `<ul class="yaml-problems">${items}</ul>` : '');
  };
  $('#yaml-validate')?.addEventListener('click', () => {
    jsonFetch('/api/settings/validate', {
      method: 'POST',
      body: JSON.stringify({ text: yamlText.value }),
    })
      .then((d) => renderYamlCheck(d.problems))
      .catch((e) => toast(`Error: ${e.message || 'Validate failed'}`, true));
  });
  // A result describes the text it was run on, so an edit makes it stale.
  yamlText?.addEventListener('input', () => {
    if (yamlCheck.firstElementChild && !yamlCheck.querySelector('.b-abort')) {
      yamlCheck.innerHTML = '<span class="badge b-abort">Not checked</span>'
        + '<span class="note">Edited since the last check.</span>';
    }
  });

  // ── Saving one field ────────────────────────────────────────────────────
  const saveSetting = (path, value) =>
    jsonFetch('/api/settings/field', {
      method: 'POST',
      body: JSON.stringify({ path, value }),
    })
      .then((d) => {
        toast('Saved');
        markChanged(path, d.changed_from_default);
        if (d.restart_required && typeof checkRestartStatus === 'function') checkRestartStatus();
      })
      .catch((e) => toast(`Error: ${e.message || 'Save failed'}`, true));

  $$('[data-setting-path]').forEach((el) => {
    const path = el.dataset.settingPath;
    const nullable = el.dataset.nullable === 'true';

    // auth.enabled is wired separately below - turning it off needs the current password
    // and clears the stored one, which the generic checkbox handler knows nothing about.
    if (path === 'auth.enabled') return;

    if (el.type === 'checkbox') {
      el.addEventListener('change', () => saveSetting(path, el.checked));
      return;
    }
    if (el.tagName === 'SELECT') {
      el.addEventListener('change', () => {
        const v = el.value;
        saveSetting(path, !isNaN(v) && v !== '' ? Number(v) : v);
      });
      return;
    }
    // blur fires whenever focus leaves the field, whether or not the value actually
    // changed - track a baseline so a no-op click-in/click-out does not trigger a save
    // (and, on the restart-required fields, a false "restart required" banner).
    let baseline = el.value;
    if (el.dataset.settingType === 'list') {
      el.addEventListener('blur', () => {
        if (el.value === baseline) return;
        baseline = el.value;
        saveSetting(path, el.value.split('\n').map((l) => l.trim()).filter(Boolean));
      });
      return;
    }
    baseline = el.value.trim();
    el.addEventListener('blur', () => {
      const v = el.value.trim();
      if (v === baseline) return;
      baseline = v;
      if (el.type === 'number') saveSetting(path, v === '' ? null : parseFloat(v));
      else saveSetting(path, nullable && v === '' ? null : v);
    });
  });

  // ── Turn off the login gate ───────────────────────────────────────────────
  // Disabling clears the stored password server-side too (app/routes/settings.py -
  // api_settings_field), so this has to confirm the current password first rather than
  // firing the generic checkbox save: a bare toggle-off would irreversibly clear a
  // password the user never wrote down with no chance to back out.
  const authToggle = $('input[data-setting-path="auth.enabled"]');
  if (authToggle) {
    authToggle.addEventListener('change', () => {
      if (authToggle.checked) {
        saveSetting('auth.enabled', true);
        return;
      }
      // Revert immediately - there is no server-rendered "declined" state for a checkbox,
      // so the DOM must not show off until the password is actually confirmed.
      authToggle.checked = true;
      const body = document.createElement('div');
      body.innerHTML = `
        <p>Turning this off clears the stored password. Turning it back on later starts
        from a fresh password, not the one you are about to enter.</p>
        <div class="form-group">
          <label for="auth-disable-password">Current password</label>
          <div class="secret-field">
            <input type="password" id="auth-disable-password" class="form-control" autocomplete="current-password">
            <button type="button" class="secret-reveal-btn" aria-label="Show what I am typing" title="Show what I am typing">
              <svg class="eye-on" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
              <svg class="eye-off" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
            </button>
          </div>
        </div>`;
      const modal = buildModal({
        title: 'Turn off the login gate',
        body,
        footer: [
          { label: 'Cancel', class: 'btn' },
          {
            label: 'Turn off', class: 'btn btn-primary', onClick: (close) => {
              const pwEl = $('#auth-disable-password', body);
              const pw = pwEl ? pwEl.value : '';
              jsonFetch('/api/settings/field', {
                method: 'POST',
                body: JSON.stringify({ path: 'auth.enabled', value: false, current_password: pw }),
              })
                .then(() => {
                  toast('Login gate turned off');
                  close();
                  setTimeout(() => window.location.reload(), 700);
                })
                .catch((e) => toast(`Error: ${e.message || 'Could not disable the login gate'}`, true));
              return false;
            },
          },
        ],
      });
      initLocalReveal(body);
      const pwEl = $('#auth-disable-password', body);
      if (pwEl) pwEl.focus();
    });
  }

  // ── Set/change the login gate password ──────────────────────────────────
  // A distinct multi-field action (current/new/confirm), not a single auto-saved
  // value, so it POSTs to its own endpoint rather than /api/settings/field. A full
  // reload on success keeps the enable toggle's disabled state and the "Set
  // password"/"Change password" heading consistent with the server, rather than
  // hand-patching several bits of DOM state in place.
  const pwForm = $('#auth-password-form');
  if (pwForm) {
    pwForm.addEventListener('submit', (e) => {
      e.preventDefault();
      const currentEl = $('#auth-current-password');
      const newEl = $('#auth-new-password');
      const confirmEl = $('#auth-confirm-password');
      const btn = pwForm.querySelector('button[type="submit"]');
      btn.disabled = true;
      jsonFetch('/api/settings/password', {
        method: 'POST',
        body: JSON.stringify({
          current_password: currentEl ? currentEl.value : '',
          new_password: newEl.value,
          confirm_password: confirmEl.value,
        }),
      })
        .then(() => {
          toast('Password saved');
          setTimeout(() => window.location.reload(), 700);
        })
        .catch((e) => {
          toast(`Error: ${e.message || 'Could not save password'}`, true);
          btn.disabled = false;
        });
    });
  }

  // ── Generate/regenerate the Home Assistant API key ────────────────────────
  // Generated server-side and shown exactly once, same show-once shape as the login
  // password - except there is nothing to confirm (nobody types it) and regenerating an
  // existing key needs its own confirm step first, since it invalidates the old one
  // immediately and the generic checkbox-toggle confirm flow above doesn't apply here.
  const haKeyBtn = $('#ha-generate-key-btn');
  if (haKeyBtn) {
    const showKeyOnceModal = (apiKey) => {
      const body = document.createElement('div');
      body.innerHTML = `
        <p>Copy this key now - it will not be shown again. Paste it into the Home Assistant
        integration's setup.</p>
        <div class="secret-field">
          <input type="text" id="ha-key-value" class="form-control" value="${escHtml(apiKey)}" readonly>
        </div>`;
      buildModal({
        title: 'Home Assistant API key',
        body,
        dismissable: false,
        onClose: () => window.location.reload(),
        footer: [
          {
            label: 'Copy', class: 'btn', onClick: () => {
              const el = body.querySelector('#ha-key-value');
              el.select();
              navigator.clipboard.writeText(el.value)
                .then(() => toast('Copied'))
                .catch(() => toast('Copy failed - clipboard access was denied', true));
              return false;
            },
          },
          { label: 'Done', class: 'btn btn-primary' },
        ],
      });
    };
    const generateKey = () => {
      haKeyBtn.disabled = true;
      jsonFetch('/api/settings/ha-api-key', { method: 'POST' })
        .then((d) => showKeyOnceModal(d.api_key))
        .catch((e) => toast(`Error: ${e.message || 'Could not generate the API key'}`, true))
        .finally(() => { haKeyBtn.disabled = false; });
    };
    haKeyBtn.addEventListener('click', () => {
      if (haKeyBtn.textContent.trim().startsWith('Regenerate')) {
        buildModal({
          title: 'Regenerate the API key?',
          body: '<p>The old key stops working immediately. Update the Home Assistant integration with the new key, or it stops polling.</p>',
          footer: [
            { label: 'Cancel', class: 'btn' },
            { label: 'Regenerate', class: 'btn btn-danger', onClick: (close) => { close(); generateKey(); return false; } },
          ],
        });
      } else {
        generateKey();
      }
    });
  }

  // ── The rail ────────────────────────────────────────────────────────────
  $('#rail').innerHTML = SECTIONS.map((sec) => `
    <button class="rail-a${sec.id === state.cur ? ' here' : ''}" type="button" data-rail="${escHtml(sec.id)}">
      <span>${escHtml(sec.title)}</span><span class="rc" data-rc="${escHtml(sec.id)}">${viewTotal(sec)}</span>
    </button>`).join('');

  // ── The mobile section picker (DESIGN.md 15.5 item 1) ───────────────────
  // The bar and the sheet are one writer over the same state, so the label on the
  // bar and the marked row in the sheet cannot disagree.
  let sheet = null;

  const sheetBodyHtml = () => {
    const q = state.q.trim();
    const rows = SECTIONS.map((sec) => {
      const n = isFiltering() ? (state.hits[sec.id] || 0) : viewTotal(sec);
      return `<button class="sp-row${sec.id === state.cur ? ' here' : ''}${state.empty[sec.id] ? ' nohit' : ''}"
        type="button" data-sec-go="${escHtml(sec.id)}">
        <span class="spr-t">${escHtml(sec.title)}</span>
        <span class="spr-n">${filterCount(n) ?? n}</span>
      </button>`;
    }).join('');
    const note = q
      ? `Counts are matches for <strong>${escHtml(state.q)}</strong>. A section with none still jumps there.`
      : state.changed ? 'Counts are the settings in each section changed from their default.'
      : (state.view === 'advanced'
        ? 'Counts are how many settings each section holds.'
        : 'Counts are the Basic settings in each section. Switch to Advanced to see the rest.');
    return `${rows}<div class="sp-note">${note}</div>`;
  };

  const syncSheet = () => {
    if (sheet) $('.modal-body', sheet).innerHTML = sheetBodyHtml();
  };

  // One writer for the bar itself.
  const syncPicker = () => {
    const sec = secById(state.cur) || SECTIONS[0];
    if (!sec) return;
    const n = isFiltering() ? (state.hits[sec.id] || 0) : viewTotal(sec);
    $('#sp-cur').textContent = sec.title;
    $('#sp-n').textContent = filterCount(n) ?? String(n);
    $('#secpick').classList.toggle('nohit', !!state.empty[sec.id]);
  };

  // While a smooth jump is in flight the spy would name every section the page
  // travels past, so the label flickers through three names on its way to the one
  // that was picked. The target is pinned until the scroll arrives at it, with a
  // timeout so a jump that never lands (a collapsed target, a cancelled scroll)
  // cannot freeze the spy pointing at a section the reader has left.
  let jumpTarget = null;
  let jumpTimer = null;

  const pinJump = (id) => {
    jumpTarget = id;
    clearTimeout(jumpTimer);
    jumpTimer = setTimeout(() => { jumpTarget = null; syncSpy(); }, 1500);
  };

  const releaseJump = () => {
    jumpTarget = null;
    clearTimeout(jumpTimer);
  };

  const markCurrent = (id) => {
    state.cur = id;
    $$('[data-rail]').forEach((b) => b.classList.toggle('here', b.dataset.rail === id));
    syncPicker();
    syncSheet();
  };

  const goToSection = (id) => {
    if (state.q.trim() && !state.hits[id]) {
      toast('No settings in that section match the current search.');
    } else if (state.changed && !state.hits[id]) {
      toast('No setting in that section is changed from its default.');
    } else if (state.empty[id]) {
      toast('Every setting in that section is Advanced. Switch to Advanced to see them.');
    }
    markCurrent(id);
    pinJump(id);
    const el = $(`#sec-${CSS.escape(id)}`);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  // A sheet IS a modal: style.css turns .modal-panel into a bottom sheet at these
  // widths (DESIGN.md 9.6), so there is deliberately no second overlay component.
  $('#secpick-btn').addEventListener('click', () => {
    if (sheet) { sheet.closeModal(); return; }
    sheet = buildModal({
      title: 'Sections',
      body: sheetBodyHtml(),
      onClose: () => { sheet = null; },
    });
  });

  document.addEventListener('click', (e) => {
    const railBtn = e.target.closest('[data-rail]');
    if (railBtn) { goToSection(railBtn.dataset.rail); return; }
    const sheetRow = e.target.closest('[data-sec-go]');
    if (sheetRow) {
      const id = sheetRow.dataset.secGo;
      // The scroll happens after the sheet has closed and the lock has been released,
      // or scrollIntoView runs against a body that is position:fixed and lands nowhere.
      if (sheet) sheet.closeModal();
      goToSection(id);
    }
  });

  // ── Search ──────────────────────────────────────────────────────────────
  const swrap = $('#swrap');
  const searchEl = $('#ssearch');
  const chip = $('#shits');
  const changedChip = $('#schanged');

  // The server stamps data-changed at render; a save reports whether the field still
  // differs from its default, so the mark follows without a reload.
  function markChanged(path, changed) {
    if (typeof changed !== 'boolean') return;
    const row = $(`.frow[data-path="${CSS.escape(path)}"]`);
    if (!row) return;
    if (changed) row.dataset.changed = 'true';
    else delete row.dataset.changed;
    applyFilters();
  }

  searchEl.placeholder = `Search ${FIELD_TOTAL} settings by name, description or key`;

  const surfaceHtml = (s) => `<div class="sres-card">
    <div class="sres-txt">
      <div class="sres-name">${escHtml(s.name)} <span class="sres-kind">${escHtml(s.kind)}</span></div>
      <div class="sres-desc">${escHtml(s.desc)}</div>
    </div>
    ${surfaceActionHtml(s)}
  </div>`;

  // Three kinds of surface result, and each states which it is rather than being inferred
  // from a missing field: a page to navigate to, a panel further down THIS page, and a
  // component that opens in place. `action` is the third - the filename designer became a
  // modal in dev/changelog/441 and no longer has a URL to link to.
  function surfaceActionHtml(s) {
    if (s.action) {
      return `<button class="btn btn-sm btn-primary" type="button" data-sact="${escHtml(s.action)}">${escHtml(s.btn)}</button>`;
    }
    if (s.href) {
      return `<a class="btn btn-sm btn-primary" href="${escHtml(s.href)}">${escHtml(s.btn)}</a>`;
    }
    return `<button class="btn btn-sm btn-primary" type="button" data-scroll="${escHtml(s.scroll)}">${escHtml(s.btn)}</button>`;
  }

  // A gated row names the fields that decide whether anything reads it (DESIGN.md 15.9):
  // 'path' needs that switch on, 'path!=value' needs that dropdown off the value. Each is
  // read from the control's live state, so flipping a switch un-dims its rows before the
  // save comes back.
  const GATED = $$('.frow[data-gated-by]');
  const rowOf = (path) => $(`.frow[data-path="${CSS.escape(path)}"]`);
  const labelOf = (path) => {
    const row = rowOf(path);
    const name = row && $('.fl-name', row);
    return name ? name.textContent.trim() : path;
  };
  // An option's name without its explanation: "Never - always lossless copy" -> "Never".
  const optionName = (opt) => opt.textContent.split(/ - | \(/)[0].trim();
  // The phrase naming an unmet predicate, or null when it holds. A predicate whose field is
  // not on the page never dims: tests/test_static_invariants.py fails that declaration.
  function unmetGate(pred) {
    const ne = pred.indexOf('!=');
    const path = ne < 0 ? pred : pred.slice(0, ne);
    const ctl = $(`[data-setting-path="${CSS.escape(path)}"]`);
    if (!ctl) return null;
    if (ne < 0) return ctl.checked ? null : `${labelOf(path)} is off`;
    const value = pred.slice(ne + 2);
    if (ctl.value !== value) return null;
    const opt = [...ctl.options].find((o) => o.value === value);
    return `${labelOf(path)} is ${opt ? optionName(opt) : value}`;
  }

  function applyFilters() {
    // Gating is its own state, not a fourth visibility input: a gated row still shows, still
    // counts and can still be a search hit or changed from default.
    GATED.forEach((row) => {
      const unmet = row.dataset.gatedBy.split(' ').map(unmetGate).filter(Boolean);
      row.classList.toggle('gated', unmet.length > 0);
      const badge = $('.gate-badge', row);
      if (badge) {
        badge.textContent = unmet.length
          ? `Not used while ${unmet.length > 1 ? `${unmet.slice(0, -1).join(', ')} and ${unmet[unmet.length - 1]}` : unmet[0]}`
          : '';
        badge.hidden = !unmet.length;
      }
    });

    const q = state.q.trim().toLowerCase();
    const onlyChanged = state.changed;
    // Either filter reaches every row whatever the view (DESIGN.md 15.9 rule 6).
    const filtering = !!q || onlyChanged;
    swrap.classList.toggle('has-text', q.length > 0);

    let total = 0;
    let crossTier = 0;
    state.hits = {};
    state.empty = {};
    SECTIONS.forEach((sec) => {
      let hits = 0;
      let hiddenByView = 0;
      let visible = 0;
      $$('.frow', sec.el).forEach((row) => {
        const matches = (!q || (row.dataset.hay || '').includes(q))
          && (!onlyChanged || row.dataset.changed === 'true');
        // The filters cross tiers: a match shows whatever the view, and the tier badge
        // (CSS, keyed on .hit) says why it is there.
        const on = filtering ? matches : inView(row);
        // .off means "not on screen right now", whichever input decided it.
        row.classList.toggle('off', !on);
        row.classList.toggle('hit', filtering && on);
        if (on) visible++;
        if (on && row.dataset.path) {
          hits++;
          if (filtering && !inView(row)) crossTier++;
        }
        if (!on && !filtering && row.dataset.path) hiddenByView++;
      });
      // The password and API-key forms filter as units, never by the query. They hold no
      // value a default could describe, so the changed filter hides them.
      $$('[data-tier]:not(.frow)', sec.el).forEach((unit) => {
        unit.hidden = onlyChanged || (!q && !inView(unit));
      });
      // A group heading over zero visible rows reads as an empty group, not a miss.
      $$('[data-sub]', sec.el).forEach((group) => {
        group.hidden = !$$('.frow', group).some((row) => !row.classList.contains('off'));
      });
      // A section nothing on screen belongs to collapses to its own head rather than
      // hiding: the rail lists every section and a page showing fewer contradicts it.
      const empty = filtering ? hits === 0 : visible === 0;
      state.hits[sec.id] = hits;
      state.empty[sec.id] = empty;
      total += hits;
      const cnt = $(`[data-cnt="${CSS.escape(sec.id)}"]`);
      if (cnt) {
        cnt.textContent = filterCount(hits)
          ?? (empty && sec.total ? plural(sec.total, 'advanced setting') : '');
      }
      const rc = $(`[data-rc="${CSS.escape(sec.id)}"]`);
      if (rc) rc.textContent = filtering ? hits : viewTotal(sec);
      const rail = $(`[data-rail="${CSS.escape(sec.id)}"]`);
      if (rail) rail.classList.toggle('nohit', empty);
      sec.el.classList.toggle('nohit', empty);
      const more = $(`[data-more="${CSS.escape(sec.id)}"]`);
      if (more) {
        const show = !filtering && !empty && hiddenByView > 0;
        more.hidden = !show;
        more.innerHTML = show
          ? `<span>${hiddenByView} more in Advanced</span>
             <button class="btn btn-sm" type="button" data-view-go="advanced">Show Advanced</button>`
          : '';
      }
    });

    page.dataset.view = state.view;
    $$('[data-view-set]').forEach((b) => {
      const on = b.dataset.viewSet === state.view;
      b.classList.toggle('on', on);
      b.setAttribute('aria-checked', on ? 'true' : 'false');
    });

    syncPicker();
    syncSheet();

    // A page or a tool has no value to have changed, so the changed filter hides them.
    const surfaces = q && !onlyChanged
      ? BOOT.surfaces.filter((s) => `${s.name} ${s.desc} ${s.hay}`.toLowerCase().includes(q))
      : [];
    const sres = $('#sres');
    sres.className = surfaces.length ? 'sres' : '';
    sres.innerHTML = surfaces.map(surfaceHtml).join('');

    const changedTotal = $$('.frow[data-path][data-changed="true"]').length;
    changedChip.classList.toggle('active', onlyChanged);
    changedChip.setAttribute('aria-pressed', onlyChanged ? 'true' : 'false');
    // Nothing to filter to is still an answer, so the chip stays and says 0. It stays
    // usable while on, or the last change reverted would strand the page filtered.
    changedChip.disabled = !onlyChanged && changedTotal === 0;
    $('#schanged-n').textContent = changedTotal;

    chip.style.display = filtering ? '' : 'none';
    // The two kinds are counted separately. Folding a page into "85 of 84 settings"
    // would be a lie about what was found.
    chip.textContent = filtering
      ? `${total} of ${FIELD_TOTAL} settings` +
        (surfaces.length ? ` + ${plural(surfaces.length, 'page')}` : '')
      : '';

    // A filter in Basic that reached Advanced rows says so above the results, with the
    // way to keep them: otherwise clearing it makes the setting just found vanish.
    const badge = '<span class="tier-badge tier-badge-on">Advanced</span>';
    const whileOn = q ? 'while you search' : 'while Changed from default is on';
    const one = crossTier === 1;
    const lead = q
      ? (one ? '1 match is an Advanced setting.' : `${crossTier} matches are Advanced settings.`)
      : (one ? '1 changed setting is Advanced.' : `${crossTier} changed settings are Advanced.`);
    $('#xtier-slot').innerHTML = crossTier
      ? `<div class="xtier"><span>${lead} ${one ? 'It is' : 'They are'} marked ${badge} and ${one ? 'shows' : 'show'} only ${whileOn}.</span>
          <button class="btn btn-sm" type="button" data-view-go="advanced">Show Advanced</button></div>`
      : '';

    // The verdict goes at the top, and only when BOTH kinds found nothing - a page
    // that says "no results" in the same view as a designer link is two answers
    // disagreeing.
    let verdict = '';
    if (filtering && total === 0 && surfaces.length === 0) {
      const what = !q ? 'No setting on this page is changed from its default.'
        : onlyChanged ? `Nothing changed from its default matches <strong>${escHtml(state.q)}</strong>.`
          : `Nothing matches <strong>${escHtml(state.q)}</strong>.`;
      const btn = q ? '<button class="btn btn-sm" type="button" id="nh-clear">Clear the search</button>'
        : '<button class="btn btn-sm" type="button" id="nh-changed-off">Show all settings</button>';
      verdict = `<div class="card no-hits">${what}
          <div style="margin-top:10px">${btn}</div></div>`;
    }
    $('#no-hits-slot').innerHTML = verdict;
  }

  // Switching view keeps what the reader clicked from where it is on screen. Anchoring to a
  // section's top was measured wrong in the browser pass: rows revealed inside a tall card,
  // above the footer that was clicked, pushed the reader far down the page.
  function setView(view, anchorEl) {
    if (view === state.view) return;
    const anchor = anchorEl && anchorEl.getClientRects().length ? anchorEl : null;
    const before = anchor ? anchor.getBoundingClientRect().top : 0;
    state.view = view;
    applyFilters();
    if (anchor) window.scrollBy(0, anchor.getBoundingClientRect().top - before);
    // The choice follows the user across browsers (DESIGN.md 3.11), so it is stored
    // server-side and stays until they switch again.
    jsonFetch(`/api/user-prefs/${encodeURIComponent(BOOT.view_pref_key)}`, {
      method: 'POST',
      body: JSON.stringify({ value: view === 'advanced' }),
    }).catch((e) => toast(`Error: ${e.message || 'Could not save the view'}`, true));
  }

  document.addEventListener('click', (e) => {
    const set = e.target.closest('[data-view-set]');
    if (set) { setView(set.dataset.viewSet, set); return; }
    const go = e.target.closest('[data-view-go]');
    if (!go) return;
    // A card footer vanishes in Advanced, so the row above it is what stays put. The
    // search notice changes no row, so it needs no anchor.
    const card = go.closest('.sec-card');
    const rows = card ? $$('.frow', card).filter((r) => !r.classList.contains('off')) : [];
    setView(go.dataset.viewGo, rows[rows.length - 1]);
  });

  // Keeps `?q=` in the address bar in sync with the live search, so any of the page's
  // several post-action `window.location.reload()` calls (turn off login gate, set
  // password, HA API key "shown once" modal close) land back on the same filtered view
  // instead of the unfiltered page. Not called from the boot-time applyFilters() or the
  // ?q= prefill below - that sequence reads an incoming q param and syncing there first
  // risks stripping it before it's read.
  function syncUrlQ() {
    const url = new URL(window.location.href);
    if (state.q) url.searchParams.set('q', state.q);
    else url.searchParams.delete('q');
    if (state.changed) url.searchParams.set('changed', '1');
    else url.searchParams.delete('changed');
    history.replaceState(null, '', url);
  }

  const clearSearch = () => {
    state.q = '';
    searchEl.value = '';
    applyFilters();
    syncUrlQ();
    searchEl.focus();
  };

  searchEl.addEventListener('input', () => { state.q = searchEl.value; applyFilters(); syncUrlQ(); });
  // One listener for every gating control. It runs after the control's own handler, which
  // is what lets the login gate's toggle put itself back before this reads it.
  $('#secs').addEventListener('change', (e) => {
    if (e.target.matches('[data-setting-path]')) applyFilters();
  });
  searchEl.addEventListener('keydown', (e) => { if (e.key === 'Escape' && searchEl.value) clearSearch(); });
  $('#sclear').addEventListener('click', clearSearch);
  // Not saved like the view: it is a question asked of the page, like a search. The
  // address bar carries it only so the page's own reloads land on the same list.
  const setChanged = (on) => { state.changed = on; applyFilters(); syncUrlQ(); };
  changedChip.addEventListener('click', () => { if (!changedChip.disabled) setChanged(!state.changed); });
  document.addEventListener('click', (e) => {
    if (e.target.closest('#nh-clear')) { clearSearch(); return; }
    if (e.target.closest('#nh-changed-off')) { setChanged(false); return; }
    // Both entry points to the designer - the Recording field row's button and the search
    // result's - go through the one openFilenameDesigner call, so the two cannot open
    // different things.
    if (e.target.closest('[data-open-fd]') || e.target.closest('[data-sact="open-fd"]')) {
      e.preventDefault();
      // The designer never reaches into this page's DOM; it reports what it saved and the
      // page updates the value it renders. That seam is what lets a Recording Profile host
      // the same component without either of them knowing about the other.
      openFilenameDesigner({
        onSave: (d) => {
          const now = $('.frow[data-path="recording.filename_template"] .tpl-now');
          if (now) now.textContent = d.template;
          markChanged('recording.filename_template', d.changed_from_default);
        },
      });
      return;
    }
    const scroller = e.target.closest('[data-scroll]');
    if (scroller) {
      const el = document.getElementById(scroller.dataset.scroll);
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  });

  // ── Scrollspy ───────────────────────────────────────────────────────────
  // The rail and the picker bar both claim to mark where you are, so they have to
  // track the scroll rather than only respond to their own clicks.
  function syncSpy() {
    if (!SECTIONS.length) return;
    // The reading line is READ OFF the same scroll-margin-top the browser uses to
    // land a jump, never written a second time here - two coupled offsets in two
    // places is the CSS defect class in CLAUDE.md, and 2px of drift is enough to
    // make the bar name the section you just left.
    const line = parseFloat(getComputedStyle(SECTIONS[0].el).scrollMarginTop || 0) + 2;
    let best = SECTIONS[0];
    SECTIONS.forEach((sec) => {
      if (sec.el.getBoundingClientRect().top <= line) best = sec;
    });
    if (jumpTarget) {
      if (best.id !== jumpTarget) return;   // still travelling; the target stays marked
      releaseJump();
    }
    if (best.id === state.cur) return;
    markCurrent(best.id);
  }
  window.addEventListener('scroll', syncSpy, { passive: true });

  // ── Boot ────────────────────────────────────────────────────────────────
  syncPicker();
  applyFilters();
  page.classList.add('ready');
  syncSpy();

  // Pre-fill the search from ?q= (the Jobs page links in this way), and the changed
  // filter from the ?changed=1 syncUrlQ() leaves for a reload.
  const params = new URLSearchParams(window.location.search);
  const urlQuery = params.get('q');
  if (urlQuery) {
    searchEl.value = urlQuery;
    state.q = urlQuery;
  }
  state.changed = params.get('changed') === '1';
  if (urlQuery || state.changed) applyFilters();
})();
