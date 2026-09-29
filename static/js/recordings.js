/* The Recordings list (templates/index.html) - DESIGN.md 3.1/3.9/3.11, mobile 9.3/9.4.

   KEEPING ITSELF CURRENT (dev/changelog/1144). The list used to be drawn once: a finished
   recording kept reading Recording with a "Stop recording" menu item, a scheduled one never
   moved to In Progress, and "in 5 min" never counted down. Two writers now, on two scales,
   and they never write the same node:

     - the ROW SET - #rec-sub and the whole .rec-list (header, sections, rows) - is swapped
       for a fresh server render when /api/nav-status's recording_signature differs from
       the one #rec-sub was rendered at, and after this page's own actions;
     - the CLOCK CELLS inside each row (the day label and the relative line) are rewritten
       once a minute from /api/recordings/times, because re-rendering every row to move
       "in 5 min" to "in 4 min" costs ~1.3 ms and 1.7 KB per recording.

   Everything the user chose lives outside the swapped markup - sort, search text, filter
   chips, visible columns, the list's sideways scroll - and is re-applied after each swap.
   Handlers are delegated to the document for the same reason: a listener bound to a row
   goes with the row.

   escHtml/jsonFetch/swapFromServer/sortChildren/bindNavClicks/buildModal/showToast/
   openLightbox and the delete-confirm helpers come from util.js; createFilterBar from
   filter-bar.js; openModal from guide.js. */

(() => {
  'use strict';

  const readJson = (id) => {
    const el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (err) { return null; }
  };

  const recSub = () => document.getElementById('rec-sub');
  const recList = () => document.querySelector('.rec-list');

  /* The empty page has no toolbar and no list, so there is no user state to keep and no
     region to swap into: a recording arriving reloads it, deliberately. The reverse -
     the last recording going - is handled in refresh() below. */
  if (!recList()) {
    window.__applyRecordingSignature = (sig) => {
      const sub = recSub();
      if (sub && typeof sig === 'string' && sig !== sub.dataset.recSig) {
        location.reload();
      }
    };
    return;
  }

  // ── Columns: visibility + order, persisted server-side (DESIGN.md 3.11) ──
  const COLUMNS = [
    { key: 'time',     label: 'Time',        width: '150px' },
    { key: 'duration', label: 'Duration',    width: '90px'  },
    { key: 'size',     label: 'Size',        width: '90px'  },
    { key: 'health',   label: 'Health',      width: '62px'  },
    { key: 'account',  label: 'Account',     width: '110px', off: true },
    { key: 'output',   label: 'Output file', width: 'minmax(140px, 1fr)', off: true },
  ];
  const savedPrefs = readJson('rec-col-prefs');
  const colState = savedPrefs && Array.isArray(savedPrefs.order)
    ? savedPrefs
    : { order: COLUMNS.map(c => c.key), hidden: COLUMNS.filter(c => c.off).map(c => c.key) };
  // tolerate prefs saved before a column existed
  COLUMNS.forEach(c => { if (!colState.order.includes(c.key)) colState.order.push(c.key); });

  // A stylesheet rather than inline styles on the rows, so rows swapped in later pick the
  // column setup up with nothing to re-apply.
  const colStyle = document.createElement('style');
  document.head.appendChild(colStyle);

  const applyColumns = () => {
    const visible = colState.order.filter(k => !colState.hidden.includes(k));
    const widths = visible.map(k => COLUMNS.find(c => c.key === k).width);
    let css = `@media (min-width: 961px) { .list-head, .rows .row { grid-template-columns: minmax(240px,2.2fr) ${widths.join(' ')} 44px; } }\n`;
    colState.order.forEach((k, i) => {
      css += `[data-col="${k}"] { order: ${i + 1}; ${colState.hidden.includes(k) ? 'display: none;' : ''} }\n`;
    });
    css += '.c-title { order: 0; } .c-actions { order: 98; } .prog { order: 99; }\n';
    colStyle.textContent = css;
  };
  applyColumns();

  const saveColumns = () => {
    jsonFetch('/api/user-prefs/recordings_columns', {
      method: 'POST',
      body: JSON.stringify({ value: colState }),
    }).catch(() => showToast('Could not save column setup', { type: 'error' }));
  };

  // Columns popover: checkbox list with drag-to-reorder grips
  const colMenu = document.getElementById('columns-menu');
  const buildColMenu = () => {
    colMenu.querySelectorAll('.col-item').forEach(el => el.remove());
    const note = colMenu.querySelector('.pop-note');
    colState.order.forEach(key => {
      const col = COLUMNS.find(c => c.key === key);
      const item = document.createElement('label');
      item.className = 'col-item';
      item.draggable = true;
      item.dataset.key = key;
      item.innerHTML = `<span class="grip">&#8942;&#8942;</span><input type="checkbox" ${colState.hidden.includes(key) ? '' : 'checked'}> ${col.label}`;
      item.querySelector('input').addEventListener('change', (e) => {
        if (e.target.checked) colState.hidden = colState.hidden.filter(k => k !== key);
        else if (!colState.hidden.includes(key)) colState.hidden.push(key);
        applyColumns();
        saveColumns();
      });
      item.addEventListener('dragstart', (e) => { e.dataTransfer.setData('text/plain', key); });
      item.addEventListener('dragover', (e) => e.preventDefault());
      item.addEventListener('drop', (e) => {
        e.preventDefault();
        const from = e.dataTransfer.getData('text/plain');
        if (!from || from === key) return;
        const order = colState.order.filter(k => k !== from);
        order.splice(order.indexOf(key), 0, from);
        colState.order = order;
        applyColumns();
        saveColumns();
        buildColMenu();
      });
      colMenu.insertBefore(item, note);
    });
  };
  buildColMenu();

  // ── Sort: clickable headers on desktop (DESIGN.md 3.1/10.3), a Sort chip on
  //    mobile where there are no column headers to click (DESIGN.md 9.4) - both
  //    drive the same sortKey/sortDir state, applied inside each section. ──
  let sortKey = 'start', sortDir = 'desc';
  const SORT_NUMERIC = { start: 1, dur: 1, size: 1, health: 1 };
  const SORT_LABEL = { start: 'Time', name: 'Name', dur: 'Duration', size: 'Size' };
  const sortChip = document.getElementById('sort-chip');

  const applySort = () => {
    document.querySelectorAll('.rec-list .rows').forEach(sec => {
      sortChildren(sec, '.row', row => {
        const v = row.dataset[{ name: 'name', start: 'start', dur: 'dur', size: 'size', health: 'health' }[sortKey]];
        return SORT_NUMERIC[sortKey] ? parseInt(v || '0', 10) : (v || '');
      }, sortDir);
    });
    document.querySelectorAll('.list-head .sortable').forEach(h => {
      const active = h.dataset.sort === sortKey;
      h.classList.toggle('sorted', active);
      h.querySelector('.sort-ind').textContent = active ? (sortDir === 'desc' ? ' ▾' : ' ▴') : '';
    });
    if (sortChip) {
      const label = SORT_LABEL[sortKey] || sortKey;
      sortChip.textContent = `Sort: ${label} ${sortDir === 'desc' ? '▾' : '▴'}`;
    }
  };
  const setSort = (key) => {
    if (sortKey === key) sortDir = sortDir === 'desc' ? 'asc' : 'desc';
    else { sortKey = key; sortDir = SORT_NUMERIC[key] ? 'desc' : 'asc'; }
    applySort();
  };
  document.addEventListener('click', (e) => {
    const h = e.target.closest('.list-head .sortable');
    if (h) setSort(h.dataset.sort);
  });
  document.querySelectorAll('#sort-menu [data-sortfield]').forEach(b => {
    b.addEventListener('click', () => setSort(b.dataset.sortfield));
  });
  applySort();

  // ── Search + filter chips ──
  // Re-read on every call rather than captured once: the row set is swapped.
  const allRows = () => Array.from(document.querySelectorAll('.rec-list .rows .row'));
  const searchInput = document.getElementById('rec-search');
  const chipsWrap = document.getElementById('filter-chips');
  const filterMenu = document.getElementById('filter-menu');
  const noResults = document.getElementById('rec-no-results');

  // The dimension registry for the shared filter bar (static/js/filter-bar.js,
  // DESIGN.md 3.11). Values inside one dimension OR together (Channel: FS1 or FS2) while
  // different dimensions AND (Channel: FS1 and Status: recording). Every value is
  // whatever the rows actually carry, so a filter can never offer a choice that matches
  // nothing.
  const rowValues = (attr) => [...new Set(allRows().map(r => r.dataset[attr]).filter(Boolean))]
    .sort().map(v => ({ v, label: v }));
  const FILTER_DIMS = [
    { k: 'status', label: 'Status', values: () => rowValues('status'),
      match: (row, v) => row.dataset.status === v },
    { k: 'channel', label: 'Channel', values: () => rowValues('channel'),
      match: (row, v) => row.dataset.channel === v },
    { k: 'account', label: 'Account', values: () => rowValues('account'),
      match: (row, v) => row.dataset.account === v },
  ];

  let filterBar = null;
  const applyFilter = () => {
    const q = searchInput.value.trim().toLowerCase();
    let visible = 0;
    allRows().forEach(row => {
      const searchMatch = !q || row.dataset.name.includes(q) || (row.dataset.channel || '').toLowerCase().includes(q);
      const show = searchMatch && filterBar.matches(row);
      row.style.display = show ? '' : 'none';
      if (show) visible++;
    });
    document.querySelectorAll('.rec-list .rows').forEach(sec => {
      const shown = Array.from(sec.children).filter(r => r.style.display !== 'none').length;
      sec.style.display = shown ? '' : 'none';
      const head = document.querySelector(`.rec-list .sec-head[data-section="${sec.dataset.section}"]`);
      if (!head) return;
      head.style.display = shown ? '' : 'none';
      // The count must track what is actually visible - a section reading "55"
      // above a single search hit states a number the page is contradicting.
      const cnt = head.querySelector('.cnt');
      if (cnt) cnt.textContent = shown;
    });
    noResults.style.display = visible === 0 ? '' : 'none';
  };
  searchInput.addEventListener('input', applyFilter);

  filterBar = createFilterBar({
    chipsEl: chipsWrap,
    menuEl: filterMenu,
    dims: FILTER_DIMS,
    rows: allRows,
    note: 'Choices inside one filter are an "or"; different filters are an "and".',
    onChange: applyFilter,
    saved: {
      chipEl: document.getElementById('saved-filters-chip'),
      menuEl: document.getElementById('saved-filters-menu'),
      list: (readJson('rec-saved-filters') || {}).list,
      prefUrl: `/api/user-prefs/${(readJson('rec-saved-filters') || {}).key}`,
    },
  });
  // A default saved filter is already on the bar by now, and the bar does not notify
  // while it is still being built.
  applyFilter();

  // ── Click targets (DESIGN.md 3.9): row -> details; thumb -> lightbox;
  //    pills are real links; the actions cell swallows its clicks ──
  // The kebab handler is delegated on document (util.js), so nothing inside a row
  // may stopPropagation on the way up - that would swallow the event before it
  // reaches document and the menu would never open. The row guards itself instead.
  const NO_NAV = '.c-actions, .ch-pill, .thumb.has-img, .menu, [data-tip]';
  bindNavClicks(document, (e) => {
    const row = e.target.closest('.rec-list .rows .row');
    return row && !e.target.closest(NO_NAV) ? row.dataset.href : null;
  });
  document.addEventListener('click', (e) => {
    const thumb = e.target.closest('.rec-list .thumb.has-img');
    if (thumb) openLightbox(thumb.dataset.shot);
  });

  // ── Live refresh (dev/changelog/1144) ──────────────────────────────────
  const TICK_MS = 60 * 1000;
  let tickedAt = Date.now();
  let ticking = false;
  let inFlight = false;
  let again = false;

  // An open row menu holds a swap - replacing the row would close it mid-choice - and the
  // next poll (or the next action) asks again.
  const busy = () => Boolean(document.querySelector('.rec-list .menu.open'));

  function refresh({ force = false } = {}) {
    if (inFlight) { again = true; return; }
    if (!force && busy()) return;
    const list = recList();
    if (!list) return;
    inFlight = true;
    const scrollX = list.scrollLeft;
    swapFromServer(['#rec-sub', '.rec-list'])
      .then((doc) => {
        // The last recording went: the server renders the empty page, which has no list to
        // swap in and none of this page's controls.
        if (!doc.querySelector('.rec-list')) { location.reload(); return; }
        const swapped = recList();
        if (swapped) swapped.scrollLeft = scrollX;
        applySort();
        // Re-offers the values the new rows carry and drops a chosen value no row carries
        // any more (DESIGN.md 3.11), then filters the new rows.
        filterBar.apply();
        tickedAt = Date.now();
      })
      .catch((err) => console.warn('Recordings list refresh failed; the next poll retries.', err))
      .finally(() => {
        inFlight = false;
        if (again) { again = false; refresh(); }
      });
  }

  window.__applyRecordingSignature = (sig) => {
    const sub = recSub();
    if (!sub || typeof sig !== 'string') return;
    if (sig !== sub.dataset.recSig) { refresh(); return; }
    tick();
  };

  /* The clock cells. Written by id into rows the swap already rendered, never adding or
     removing one - which rows exist is the swap's job. Skipped in a background tab; the
     first poll after the tab comes back finds the cells a minute stale and ticks then. */
  function tick() {
    if (document.hidden || ticking || Date.now() - tickedAt < TICK_MS) return;
    ticking = true;
    jsonFetch('/api/recordings/times')
      .then((d) => {
        const rows = (d && d.rows) || {};
        document.querySelectorAll('.rec-list .rows .row[data-id]').forEach((row) => {
          const t = rows[row.dataset.id];
          if (!t) return;
          const day = row.querySelector('[data-col="time"] .day');
          const rel = row.querySelector('[data-col="time"] .rel');
          if (day) day.textContent = t.day;
          if (rel) rel.textContent = t.rel;
        });
        tickedAt = Date.now();
      })
      .catch((err) => console.warn('Recordings list time refresh failed; the next poll retries.', err))
      .finally(() => { ticking = false; });
  }

  // Saving an edited schedule from the shared record modal (guide.js).
  window.onScheduleSaved = () => refresh({ force: true });

  // ── Kebab actions (confirm anatomy per DESIGN.md section 4) ──
  const ACTIONS = {
    stop: {
      title: 'Stop recording',
      body: (name) => `Capture of "${escHtml(name)}" will end now and its segments will be joined into the final file. It can't be resumed.`,
      verb: 'Stop recording',
      url: (id) => `/recordings/${id}/stop-json`,
    },
    cancel: {
      title: 'Cancel scheduled recording',
      body: (name) => `"${escHtml(name)}" will be removed.`,
      verb: 'Cancel recording',
      url: (id) => `/recordings/${id}/cancel-json`,
    },
    delete: {
      title: 'Delete recording',
      // Complete markup rather than a prose sentence: the delete confirm carries the
      // "delete the files" switch, and a <label> cannot live inside the <p> wrapper the
      // other specs get.
      bodyHtml: (name) => deleteRecordingBody(`"${escHtml(name)}" will be removed from ChannelBin.`),
      verb: 'Delete',
      url: (id) => `/recordings/${id}/delete-json`,
      payload: () => ({ delete_files: deleteRecordingWantsFiles() }),
    },
  };
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.rec-list [data-act]');
    if (!btn) return;
    // Read now: the row this button sits in may be swapped out while its dialog is open.
    const id = btn.dataset.id;
    const name = btn.dataset.recName;
    if (btn.dataset.act === 'edit-schedule') {
      const d = JSON.parse(btn.dataset.edit);
      openModal({
        has_recording: true,
        recording_id: Number(id),
        recording_status: 'SCHEDULED',
        recording_start_time: d.start_iso,
        recording_stop_time: d.stop_iso,
        recording_profile_id: d.profile_id,
        suggested_name: name,
        title: name,
        stream_url: d.url,
        channel_id: d.channel_id,
        group_id: d.group_id,
        id: null,
        description: null,
      }, null);
      return;
    }
    const spec = ACTIONS[btn.dataset.act];
    if (!spec) return;
    buildModal({
      title: spec.title,
      body: spec.bodyHtml
        ? spec.bodyHtml(name)
        : `<p style="font-size:.9rem">${spec.body(name)}</p>`,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (close) => { close(); } },
        { label: spec.verb, class: 'btn btn-danger', onClick: (close) => {
            // Body read before the request goes out; `return false` keeps the modal (and
            // so the delete confirm's switch) mounted while it is in flight.
            const opts = { method: 'POST' };
            if (spec.payload) opts.body = JSON.stringify(spec.payload());
            jsonFetch(spec.url(id), opts)
              .then(() => { close(); refresh({ force: true }); })
              .catch(err => { close(); showToast(err.message, { type: 'error' }); });
            return false;
          } },
      ],
    });
  });
})();
