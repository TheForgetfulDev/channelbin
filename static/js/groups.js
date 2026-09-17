// Groups tab (DESIGN.md §14): sectioned channel-group / health-check list.
// Rows are server-rendered (channels/groups.html); this file adds search, the
// 6-dimension OR-within/AND-across filter, per-section sort, per-group member-table
// sort, and the kebab actions (some navigate/POST directly, others open a modal).
(() => {
  'use strict';
  const CFG = window.GROUPS_CONFIG;

  // ── Search + filter (§14.6) ──────────────────────────────────────────
  const allItems = Array.from(document.querySelectorAll('.grp-item'));
  const searchInput = document.getElementById('grp-search');
  const searchWrap = document.getElementById('search-wrap');
  const searchClear = document.getElementById('search-clear');
  const noResults = document.getElementById('grp-no-results');
  const filterCountEl = document.getElementById('filter-count');
  const chipsWrap = document.getElementById('filter-chips');
  const filterMenu = document.getElementById('filter-menu');
  // One noun, one section. A group used only for health checking is a group whose format
  // strategy is health_check_only, which the Purpose filter below reads.
  const SECTIONS = ['groups'];

  // A row carries its own health class plus its schedule's (data-healths), so one row
  // matches if EITHER does.
  const hasToken = (row, attr, v) => (row.dataset[attr] || '').split(' ').includes(v);

  // The dimension registry for the shared filter bar (static/js/filter-bar.js,
  // DESIGN.md 3.11). `row` here is the server-rendered .grp-item node, so every match
  // reads its data attributes.
  const FILTER_DIMS = [
    { k: 'purpose', label: 'Purpose', values: [
      { v: 'recording', label: 'Records' },
      { v: 'health_check_only', label: 'Health check only' },
    ], match: (row, v) => (v === 'health_check_only'
      ? row.dataset.strategy === 'health_check_only'
      : row.dataset.strategy !== 'health_check_only') },
    { k: 'health', label: 'Health', values: [
      { v: 'st-bad', label: 'Failing' },
      { v: 'st-warn', label: 'Warning' },
      { v: 'st-ok', label: 'Healthy' },
      { v: 'st-run', label: 'Running now' },
      { v: 'st-none', label: 'Unknown' },
    ], match: (row, v) => hasToken(row, 'healths', v) },
    { k: 'account', label: 'Account', values: null, match: (row, v) => (row.dataset.accounts || '').split('|').includes(v) },
    { k: 'guide', label: 'TV Guide', values: [
      { v: 'in', label: 'In the guide' },
      { v: 'out', label: 'Not in the guide' },
    ], match: (row, v) => row.dataset.guide === v },
    { k: 'check', label: 'Health check', values: [
      { v: 'sched', label: 'On a schedule' },
      { v: 'oneoff', label: 'One-off only' },
      { v: 'none', label: 'No schedule' },
    ], match: (row, v) => row.dataset.check === v },
    { k: 'issue', label: 'Needs attention', values: [
      { v: 'mismatch', label: 'Mixed format' },
      { v: 'unmon', label: 'Not monitored' },
      { v: 'empty', label: 'No channels' },
      { v: 'untested', label: 'Has untested channels' },
    ], match: (row, v) => (row.dataset.issues || '').split(' ').includes(v) },
  ];
  // Account values are the accounts actually present across every row, not a fixed list.
  const accountValues = [...new Set(allItems.flatMap(r => (r.dataset.accounts || '').split('|').filter(Boolean)))].sort();
  FILTER_DIMS.find(d => d.k === 'account').values = accountValues.map(a => ({ v: a, label: a }));

  const filterBar = createFilterBar({
    chipsEl: chipsWrap,
    menuEl: filterMenu,
    dims: FILTER_DIMS,
    rows: () => allItems,
    note: 'Choices inside one filter are an "or"; different filters are an "and". '
      + 'Filtering matches groups, never single channels.',
    onChange: () => applyFilter(),
  });

  const setSearchTerm = (v, writeInput) => {
    if (writeInput) searchInput.value = v;
    searchWrap.classList.toggle('has-text', searchInput.value.length > 0);
    applyFilter();
  };
  searchInput.addEventListener('input', () => applyFilter());
  searchClear.addEventListener('click', () => { setSearchTerm('', true); searchInput.focus(); });
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && e.target.value) { setSearchTerm('', true); e.stopPropagation(); }
  });

  function applyFilter() {
    const q = searchInput.value.trim().toLowerCase();
    searchWrap.classList.toggle('has-text', searchInput.value.length > 0);
    let visible = 0;
    allItems.forEach(row => {
      const show = (!q || (row.dataset.search || '').includes(q)) && filterBar.matches(row);
      row.style.display = show ? '' : 'none';
      if (show) visible++;
    });
    SECTIONS.forEach(section => {
      const list = document.getElementById(`grp-list-${section}`);
      if (!list) return;
      const shown = Array.from(list.children).filter(r => r.style.display !== 'none').length;
      const head = document.querySelector(`.sec-head[data-section="${section}"]`);
      list.style.display = shown ? '' : 'none';
      if (head) {
        head.style.display = shown ? '' : 'none';
        const cnt = head.querySelector('.cnt');
        if (cnt) cnt.textContent = shown;
      }
    });
    noResults.style.display = visible === 0 ? '' : 'none';
    filterCountEl.textContent = `${visible} of ${allItems.length} shown`;
    syncUrl();
  }

  document.getElementById('grp-clear-filters')?.addEventListener('click', () => {
    filterBar.clear();
    filterBar.render();
    setSearchTerm('', true);
  });

  // ── Per-section sort (§14.1/§10.3) ───────────────────────────────────
  const SORT_SPECS = {
    name: { desc: false, get: (r) => r.dataset.name },
    members: { desc: true, get: (r) => parseInt(r.dataset.membersCount || '0', 10) },
    health: { desc: false, get: (r) => ({ 'st-bad': 0, 'st-run': 1, 'st-warn': 2, 'st-ok': 3, 'st-none': 4 }[r.dataset.health]) },
    activity: { desc: true, get: (r) => r.dataset.activity || null },
    guide: { desc: false, get: (r) => (r.dataset.guide === 'in' ? 0 : 1) },
    checks: { desc: true, get: (r) => parseInt(r.dataset.checksCount || '0', 10) },
  };
  const SORT_LABELS = { name: 'Group name', members: 'Number of channels', health: 'Health (worst first)',
    activity: 'Last tested', guide: 'In the TV Guide', checks: 'Health checks attached' };
  const sectionSort = { groups: { k: 'name', dir: 1 } };

  // ── URL-persisted state (search/filter/sort) ──────────────────────────
  // Purely client-side (query string only, never a server round-trip): restores what a
  // reload would otherwise reset, and makes the current view bookmarkable/shareable.
  // Member-table sort (below) is intentionally left out - it's a per-group detail sort,
  // not the section-level state a reload resets.
  function syncUrl() {
    const params = new URLSearchParams();
    const q = searchInput.value.trim();
    if (q) params.set('q', q);
    filterBar.entries().forEach(([k, v]) => params.append('f', `${k}:${v}`));
    SECTIONS.forEach(section => {
      const st = sectionSort[section];
      if (st.k !== 'name' || st.dir !== 1) params.append('sort', `${section}:${st.k}:${st.dir}`);
    });
    const qs = params.toString();
    history.replaceState(null, '', qs ? `${window.location.pathname}?${qs}` : window.location.pathname);
  }
  function initFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const q = params.get('q');
    if (q) searchInput.value = q;
    filterBar.setFrom(params.getAll('f').map(entry => {
      const i = entry.indexOf(':');
      return i === -1 ? null : [entry.slice(0, i), entry.slice(i + 1)];
    }).filter(Boolean));
    params.getAll('sort').forEach(entry => {
      const [section, key, dir] = entry.split(':');
      if (SECTIONS.includes(section) && SORT_SPECS[key]) {
        sectionSort[section] = { k: key, dir: dir === '-1' ? -1 : 1 };
      }
    });
  }
  initFromUrl();
  filterBar.render();
  applyFilter();

  // sortChildren's single-key model can't express "pinned row always first, then a
  // mixed string/number field, with nulls always last regardless of direction" -
  // Infinity sentinels compare as NaN against strings (name sort), so they silently
  // no-op instead of pinning anything. A real comparator does all three correctly.
  function applySort(section) {
    const list = document.getElementById(`grp-list-${section}`);
    if (!list) return;
    const st = sectionSort[section];
    const spec = SORT_SPECS[st.k];
    const mult = (spec.desc ? -1 : 1) * st.dir;
    const items = Array.from(list.querySelectorAll(':scope > .grp-item'));
    items.sort((a, b) => {
      if (a.dataset.system !== b.dataset.system) return a.dataset.system === '1' ? -1 : 1;
      const av = spec.get(a), bv = spec.get(b);
      const aNull = av === null || av === undefined || av === '';
      const bNull = bv === null || bv === undefined || bv === '';
      if (aNull && bNull) return a.dataset.name.localeCompare(b.dataset.name);
      if (aNull) return 1;
      if (bNull) return -1;
      const c = typeof av === 'string' ? av.localeCompare(bv) : (av < bv ? -1 : (av > bv ? 1 : 0));
      if (c) return c * mult;
      return a.dataset.name.localeCompare(b.dataset.name);
    });
    items.forEach(el => list.appendChild(el));
    const chip = document.getElementById(`sort-chip-${section}`);
    if (chip) {
      const descNow = (spec.desc ? 1 : -1) * st.dir > 0;
      chip.textContent = `Sort: ${SORT_LABELS[st.k]} ${descNow ? '▾' : '▴'}`;
    }
    syncUrl();
  }
  SECTIONS.forEach(section => {
    const menu = document.getElementById(`sort-menu-${section}`);
    if (!menu) return;
    menu.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-gsort]');
      if (!btn) return;
      const key = btn.dataset.gsort;
      const st = sectionSort[section];
      if (st.k === key) st.dir = -st.dir; else { st.k = key; st.dir = 1; }
      applySort(section);
    });
    applySort(section);
  });

  // ── Per-group member-table sort ──────────────────────────────────────
  const MEMBER_STATUS_RANK = { FAIL: 0, WARN: 1, PASS: 2, WAITING: 3 };
  const memberSort = {};  // groupId -> {k, dir}
  document.body.addEventListener('click', (e) => {
    const th = e.target.closest('[data-msort]');
    if (!th) return;
    const table = th.closest('table.grp-mtable');
    const gid = table.dataset.mtable;
    const key = th.dataset.msort;
    const cur = memberSort[gid];
    const dir = (cur && cur.k === key) ? -cur.dir : 1;
    memberSort[gid] = { k: key, dir };
    table.querySelectorAll('th.sortable').forEach(h => {
      h.classList.toggle('sorted', h === th);
      h.querySelector('.arr').textContent = h === th ? (dir === 1 ? ' ▴' : ' ▾') : '';
    });
    const tbody = table.querySelector('tbody');
    sortChildren(tbody, 'tr', (row) => {
      if (key === 'status') {
        const v = MEMBER_STATUS_RANK[row.dataset.status];
        return v === undefined ? Infinity : v;
      }
      if (key === 'name') return row.dataset.name;
      if (key === 'tested') {
        const raw = row.dataset.tested;
        // Date.parse, not the raw ISO string - a string compared against the
        // Infinity sentinel below coerces to NaN and silently stops sorting.
        return raw ? Date.parse(raw) : (dir === 1 ? Infinity : -Infinity);
      }
      const raw = row.dataset[key];
      return raw === '' || raw === undefined ? (dir === 1 ? Infinity : -Infinity) : parseFloat(raw);
    }, dir === 1 ? 'asc' : 'desc');
  });

  // ── Row navigates to detail; disclosure triangle expands the members ──
  // The triangle (.grp-expand) and the row body ([data-expand]) do different things.
  // The triangle is checked first: it sits beside .grp-row in .grp-summary, not inside
  // it, so a click on it never matches the row branch.
  document.body.addEventListener('click', (e) => {
    const caret = e.target.closest('.grp-expand');
    if (caret) {
      if (caret.disabled) return;
      const item = caret.closest('.grp-item');
      const detail = item.querySelector('.grp-detail');
      const open = detail.hidden;
      detail.hidden = !open;
      caret.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
  });
  bindNavClicks(document.body, (e) => {
    if (e.target.closest('.grp-expand')) return null;
    // Check chips navigate to the check's results page.
    const chip = e.target.closest('[data-menu-check]');
    if (chip) return `/channels/health-checks/${chip.dataset.menuCheck.split(':')[1]}`;
    const row = e.target.closest('[data-expand]');
    if (!row) return null;
    // Real action elements (kebab, attach chip, channel links) inside the row swallow the
    // click and must not trigger navigation.
    if (e.target.closest('.menu, [data-menu], [data-act], .ch-pill')) return null;
    return row.dataset.detailUrl || null;
  });

  // ── Kebab actions ─────────────────────────────────────────────────────
  function payloadFor(groupId) {
    const item = document.querySelector(`.grp-item[data-group="${groupId}"]`);
    return item ? JSON.parse(item.dataset.payload) : null;
  }

  function reload() { window.location.reload(); }

  // Deleting a group used to reload() the whole page, which reset search/filter/sort and
  // scroll position along with it (dev/changelog/570). Removing just the row keeps all of
  // that untouched; applyFilter() re-derives section counts and the empty-state note.
  function removeGroupRow(groupId) {
    const item = document.querySelector(`.grp-item[data-group="${groupId}"]`);
    if (!item) return;
    const idx = allItems.indexOf(item);
    if (idx !== -1) allItems.splice(idx, 1);
    item.remove();
    applyFilter();
  }

  document.body.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const act = btn.dataset.act;
    const gid = parseInt(btn.dataset.group, 10);

    if (act === 'guide-toggle') {
      jsonFetch(CFG.guideToggleUrlBase + gid + '/guide-toggle', { method: 'POST' })
        .then(reload).catch(err => showToast(err.message, { type: 'error' }));
      return;
    }
    if (act === 'create-check') { openCreateCheck(gid); return; }
    if (act === 'run-check') {
      const jobId = btn.dataset.job;
      jsonFetch(CFG.startJobUrlBase + jobId + '/start', { method: 'POST' })
        .then(() => { showToast('Health check started.'); reload(); })
        .catch(err => showToast(err.message, { type: 'error' }));
      return;
    }
    if (act === 'clone') {
      openCloneModal(gid, {
        existingNames: CFG.groupNames || [],
        resolutionOptions: CFG.resolutionOptions || [],
        fpsOptions: CFG.fpsOptions || [],
        profiles: CFG.checkProfiles.profiles,
        profilesUrl: CFG.profilesUrl,
        testerBusy: CFG.testerBusy,
        windowSettingsUrl: CFG.windowSettingsUrl,
        scheduleTemplateId: 'cc-schedule-fields',
        schedulePrefix: 'ccsched',
        onDone: reload,
      });
      return;
    }
    if (act === 'delete') { openDeleteModal(gid); return; }
  });

  // ── + New Group modal ────────────────────────────────────────────────
  document.getElementById('btn-new-group').addEventListener('click', openNewGroupModal);

  function openNewGroupModal() {
    const body = document.createElement('div');
    body.innerHTML =
      `<div class="form-group"><label>Name</label><input class="form-control" id="ng-name" placeholder="e.g. Fox Sports 2"></div>` +
      `<div class="notice notice-info">You will add channels next, from Browse or from this
        group's page. Every member starts with <strong>Health check on</strong> and
        <strong>Recording off</strong> &mdash; run a check first, then turn Recording on for the
        feeds you want to record from.</div>`;

    const modal = buildModal({
      title: 'New group',
      body,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (close) => close() },
        { label: 'Create', class: 'btn btn-primary', onClick: (close) => {
          const name = body.querySelector('#ng-name').value.trim();
          if (!name) { showToast('Give the group a name first.', { type: 'error' }); return false; }
          jsonFetch(CFG.createGroupUrl, { method: 'POST', body: JSON.stringify({ name }) })
            .then((resp) => {
              showToast(`Created "${name}".`);
              if (resp && resp.detail_url) window.location = resp.detail_url;  // nav-ok: redirect after creating a group
              else reload();
            })
            .catch(err => showToast(err.message, { type: 'error' }));
          return true;
        } },
      ],
    });
    return modal;
  }

  // No "create a channel group from this health check" entry point: a health check is a
  // schedule a group already carries, so there is nothing to promote
  // (DESIGN-channel-groups-model.md DECIDED 2, dev/changelog/752). clone-modal.js still
  // opens create-group-modal.js, for cloning a group.

  // The dialog itself is group-delete.js, shared with the group detail page: the same
  // action, the same copy, and the same handling of a delete the server refuses because
  // the group is recording or has recordings scheduled.
  function openDeleteModal(groupId) {
    const g = payloadFor(groupId);
    openDeleteGroupModal({
      groupName: g.name,
      deleteUrl: CFG.deleteUrlBase + groupId + '/delete',
      attachedChecks: g.attached_checks || [],
      onDeleted: () => { showToast(`"${g.name}" deleted.`); removeGroupRow(groupId); },
      onError: (msg) => showToast(msg, { type: 'error' }),
    });
  }

  // The modal itself lives in check-modal.js - the detail page opens the same one.
  function openCreateCheck(groupId) {
    const g = payloadFor(groupId);
    openCreateCheckModal({
      groupId: g.id,
      groupName: g.name,
      memberCount: g.member_count,
      profiles: CFG.checkProfiles.profiles,
      profilesUrl: CFG.profilesUrl,
      inheritedCheck: (g.checks || []).find(c => c.inherited) || null,
      hasOwnCheck: (g.checks || []).some(c => !c.inherited),
      inGuide: g.in_guide,
      testerBusy: CFG.testerBusy,
      windowSettingsUrl: CFG.windowSettingsUrl,
      scheduleTemplateId: 'cc-schedule-fields',
      schedulePrefix: 'ccsched',
      // A health check is a schedule its group carries, so it has nothing to name - the
      // route derives the job name from the group (dev/changelog/831).
      nameless: true,
      onDone: reload,
    });
  }
})();
