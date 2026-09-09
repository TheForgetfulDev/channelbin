/* Log viewer (templates/logs.html).
   Rollout: dev/changelog/447. DESIGN.md section 16.

   Lifted out of the template, where it lived as an inline IIFE. Two rules shape
   everything below:

   ONE WRITER FOR THE LOG REGION. appendRow() is the only function that puts a
   node into #log-box; the filter only toggles the `hidden` attribute on rows
   that are already there. That is what keeps the stream and the filter from
   both owning the list (DESIGN.md 16.6), and it is also why a filter change
   cannot destroy a text selection the user is mid-copy on
   (dev/docs/BUGS.md 2026-07-18, guarded by tests/test_static_invariants.py).

   COUNTS ARE INCREMENTAL. `total` and `shown` are maintained as rows arrive and
   are trimmed, rather than recounted per line: the box holds up to 5,000 nodes
   and a busy sync writes several lines a second, so a querySelectorAll on every
   append is 5,000 nodes of work per line. The filter, which is user-initiated,
   recounts by walking once. */
(() => {
  'use strict';

  /* The source vocabulary. `hidden: true` means "known, but off by default
     because it is noisy" - the chip still renders, so nothing is invisible.
     A source that is not on this list still gets a chip the moment a line from
     it arrives, which is how a new logger becomes visible without an edit here.
     dev/mockups/build29.py extracts this list by regex to build the mockups
     against the real vocabulary - keep the literal shape if you touch it. */
  const KNOWN_SOURCES = [
    { id: 'app.watchdog',                  label: 'Watchdog',      hidden: false },
    { id: 'app.recorder',                  label: 'Recorder',      hidden: false },
    { id: 'app.concatenator',              label: 'Concat',        hidden: false },
    { id: 'app.scheduler',                 label: 'Scheduler',     hidden: false },
    { id: 'app.xtream',                    label: 'Xtream',        hidden: false },
    { id: 'app.channel_tester',            label: 'Ch. Tester',    hidden: false },
    { id: 'app.postprocessor',             label: 'Postprocessor', hidden: false },
    { id: 'app.config_backup',             label: 'Config Backup', hidden: false },
    { id: 'app.routes.recordings',         label: 'Routes',        hidden: false },
    { id: 'werkzeug',                      label: 'HTTP',          hidden: true  },
    { id: 'apscheduler.scheduler',         label: 'APScheduler',   hidden: true  },
    { id: 'apscheduler.executors.default', label: 'APSched Exec',  hidden: true  },
    { id: 'root',                          label: 'Root',          hidden: false },
    { id: 'app',                           label: 'App',           hidden: false },
  ];

  /* Label is what the chip and the row show; id is what the record carries.
     WARNING/CRITICAL are abbreviated because the level column is a fixed 8.5ch
     and the full words are what pushed it wider than the timestamp. */
  const LEVELS = [
    { id: 'DEBUG',    label: 'DEBUG' },
    { id: 'INFO',     label: 'INFO' },
    { id: 'WARNING',  label: 'WARN' },
    { id: 'ERROR',    label: 'ERROR' },
    { id: 'CRITICAL', label: 'CRIT' },
  ];

  const MAX_ROWS = 5000;

  const SOURCE_MAP = {};
  KNOWN_SOURCES.forEach((s) => { SOURCE_MAP[s.id] = s; });
  const LEVEL_MAP = {};
  LEVELS.forEach((l) => { LEVEL_MAP[l.id] = l.label; });

  const $ = (id) => document.getElementById(id);

  const box = $('log-box');
  const srcChips = $('src-chips');
  const lvChips = $('lv-chips');
  const searchWrap = $('log-search-wrap');
  const searchEl = $('log-search');
  const bottomBtn = $('btn-bottom');

  const activeSources = new Set(KNOWN_SOURCES.filter((s) => !s.hidden).map((s) => s.id));
  const activeLevels = new Set(LEVELS.map((l) => l.id));
  // Order of first appearance, so a source discovered at run time lands at the
  // end of the chip row instead of reshuffling the known ones.
  const seenSources = KNOWN_SOURCES.map((s) => s.id);
  const sourceCounts = {};
  // source id -> the chip's count node, refreshed whenever the chips are drawn.
  const countNodes = new Map();

  let searchText = '';
  let stream = null;
  let liveEnabled = true;
  let atBottom = true;
  let sheet = null;
  let total = 0;
  let shown = 0;

  // ── Vocabulary ──────────────────────────────────────────────────────────

  function srcLabel(id) {
    const known = SOURCE_MAP[id];
    if (known) return known.label;
    return id.replace(/^app\.routes\./, '').replace(/^app\./, '').replace(/_/g, ' ');
  }

  // ── Chips ───────────────────────────────────────────────────────────────

  function renderSourceChips() {
    srcChips.innerHTML = seenSources.map((id) => {
      const known = SOURCE_MAP[id];
      const tip = id + (known && known.hidden ? '\nOff by default - noisy.' : '');
      return `<button class="chip${activeSources.has(id) ? ' active' : ''}" type="button"
        data-src="${escHtml(id)}" data-tip="${escHtml(tip)}">${escHtml(srcLabel(id))}
        <span class="cn" data-cn="${escHtml(id)}">${sourceCounts[id] || 0}</span></button>`;
    }).join('');
    // Held by reference rather than looked up per line: a source id contains dots,
    // so a selector would need escaping, and this runs once per arriving line.
    countNodes.clear();
    srcChips.querySelectorAll('.cn').forEach((el) => countNodes.set(el.dataset.cn, el));
  }

  function renderLevelChips() {
    lvChips.innerHTML = LEVELS.map((l) =>
      `<button class="chip lv${activeLevels.has(l.id) ? ' active' : ''}" type="button"
        data-level="${escHtml(l.id)}">${escHtml(l.label)}</button>`).join('');
  }

  // A source nobody declared is still a source. Returns true when the chip row
  // had to grow, so the caller can redraw it once rather than per line.
  function ensureSource(id) {
    if (seenSources.includes(id)) return false;
    seenSources.push(id);
    activeSources.add(id);
    return true;
  }

  // ── Filtering ───────────────────────────────────────────────────────────

  function rowVisible(row) {
    if (!activeLevels.has(row.dataset.level)) return false;
    if (!activeSources.has(row.dataset.source)) return false;
    if (searchText && !row.dataset.msg.includes(searchText)) return false;
    return true;
  }

  /* The filter half of the one-writer rule: it toggles visibility on rows that
     already exist and never creates, removes or rewrites one. */
  function applyFilters() {
    shown = 0;
    box.querySelectorAll('.log-row').forEach((row) => {
      const vis = rowVisible(row);
      row.hidden = !vis;
      if (vis) shown += 1;
    });
    syncCounts();
  }

  function filterSummary() {
    const parts = [];
    parts.push(activeLevels.size === LEVELS.length ? 'All levels'
      : activeLevels.size === 0 ? 'No levels'
        : LEVELS.filter((l) => activeLevels.has(l.id)).map((l) => l.label).join(', '));
    const atDefault = seenSources.every((id) => {
      const known = SOURCE_MAP[id];
      return activeSources.has(id) === !(known && known.hidden);
    });
    parts.push(activeSources.size === seenSources.length ? 'all sources'
      : atDefault ? 'default sources'
        : `${activeSources.size} of ${seenSources.length} sources`);
    if (searchText) parts.push(`"${searchText}"`);
    return parts.join(' · ');
  }

  /* Any count rendered next to filterable rows is recomputed by the filter,
     never left at the total it was first written with. */
  function syncCounts() {
    $('log-count').textContent = total === 0 ? ''
      : shown === total ? `${total.toLocaleString()} lines`
        : `${shown.toLocaleString()} of ${total.toLocaleString()} lines`;
    $('lf-cur').textContent = filterSummary();
    $('logfilter-btn').classList.toggle('on', shown < total);
    srcChips.querySelectorAll('.chip').forEach((c) => {
      c.classList.toggle('active', activeSources.has(c.dataset.src));
    });
    lvChips.querySelectorAll('.chip').forEach((c) => {
      c.classList.toggle('active', activeLevels.has(c.dataset.level));
    });
  }

  function bumpSourceCount(id, delta) {
    sourceCounts[id] = (sourceCounts[id] || 0) + delta;
    const el = countNodes.get(id);
    if (el) el.textContent = sourceCounts[id];
  }

  // ── Rendering ───────────────────────────────────────────────────────────

  function buildRow(r) {
    const row = document.createElement('div');
    const bad = r.level === 'ERROR' || r.level === 'CRITICAL';
    const warn = r.level === 'WARNING';
    // DESIGN.md 16.1: rule + tint on the row, so a failure is findable while
    // scrolling past. `sev` is the row's own class - the level never rides on
    // the row as a class, because .log-lv.ERROR is the LEVEL COLUMN's rule.
    row.className = `log-row${bad ? ' sev' : warn ? ' sev warn' : ''}`;
    row.dataset.level = r.level;
    row.dataset.source = r.source;
    row.dataset.msg = `${r.source} ${r.message}`.toLowerCase();

    const time = r.ts ? r.ts.substring(11, 19) : '';
    row.innerHTML =
      `<span class="log-ts" data-tip="${escHtml(r.ts || '')}">${escHtml(time)}</span>` +
      `<span class="log-lv ${escHtml(r.level)}">${escHtml(LEVEL_MAP[r.level] || r.level)}</span>` +
      `<span class="log-src" data-tip="${escHtml(r.source)}">${escHtml(srcLabel(r.source))}</span>` +
      `<span class="log-msg">${escHtml(r.message)}</span>`;

    row.hidden = !rowVisible(row);
    return row;
  }

  /* The only function that puts a node into the log region. */
  function appendRow(r) {
    const empty = box.querySelector('.log-empty');
    if (empty) empty.remove();
    if (ensureSource(r.source)) renderSourceChips();

    const row = buildRow(r);
    box.appendChild(row);
    total += 1;
    if (!row.hidden) shown += 1;
    bumpSourceCount(r.source, 1);
    syncCounts();

    // While the user has text highlighted in the log, hold the view still and defer the row
    // cap: scrolling would drag the highlighted line off screen mid-copy, and trimming the
    // top row would collapse a selection that covers it. Both resume once the selection is
    // cleared - the cap is deliberately soft, so briefly overshooting MAX_ROWS is fine.
    syncBottomBtn();
    if (hasSelectionIn(box)) return;

    if (atBottom && !row.hidden) box.scrollTop = box.scrollHeight;

    while (total > MAX_ROWS) {
      const first = box.querySelector('.log-row');
      if (!first) break;
      total -= 1;
      if (!first.hidden) shown -= 1;
      bumpSourceCount(first.dataset.source, -1);
      first.remove();
    }
    syncCounts();
  }

  // ── Auto-scroll ─────────────────────────────────────────────────────────

  // Single updater for the button's visibility - it means "the view is not tracking the tail",
  // which is true both when scrolled up and when a selection is holding the view still.
  function syncBottomBtn() {
    bottomBtn.hidden = !(!atBottom || hasSelectionIn(box));
  }

  box.addEventListener('scroll', () => {
    atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    syncBottomBtn();
  });

  // A click that clears the selection releases the hold, so re-sync from the same place.
  document.addEventListener('selectionchange', syncBottomBtn);

  bottomBtn.addEventListener('click', () => {
    box.scrollTop = box.scrollHeight;
    atBottom = true;
    syncBottomBtn();
  });

  // ── SSE stream ──────────────────────────────────────────────────────────

  function setStatus(text, cls, pulse = false) {
    const el = $('log-status');
    el.className = `badge ${cls}`;
    el.innerHTML = (pulse ? '<span class="pulse"></span>' : '') + escHtml(text);
  }

  function startStream() {
    if (stream) stream.close();
    liveEnabled = true;
    $('btn-live').textContent = 'Stop';
    setStatus('Connecting', 'badge-scheduled');
    stream = connectSSE('/api/logs/stream', {
      onOpen: () => setStatus('Live', 'badge-in_progress', true),
      onMessage: (e) => {
        let rec = null;
        try {
          rec = JSON.parse(e.data);
        } catch (err) {
          // A malformed frame is the stream's problem, not this row's - drop the
          // frame and say so once, rather than tearing the whole view down.
          console.warn('Unparseable log frame', err);
          return;
        }
        appendRow(rec);
      },
      // connectSSE reconnects on its own, so this is not a dead end and must not
      // be labelled as one.
      onError: () => setStatus('Reconnecting', 'badge-aborted'),
    });
  }

  function stopStream() {
    if (stream) { stream.close(); stream = null; }
    liveEnabled = false;
    $('btn-live').textContent = 'Start';
    setStatus('Stopped', 'badge-scheduled');
  }

  $('btn-live').addEventListener('click', () => {
    if (liveEnabled) {
      stopStream();
      showToast('Stopped. The lines already loaded stay on screen.');
    } else {
      startStream();
    }
  });

  // ── Clear ───────────────────────────────────────────────────────────────

  // Emptying the whole region is the one place a wholesale rewrite is right - it
  // is what the user asked for. Every counter the append path maintains has to be
  // reset with it, or the foot keeps claiming lines that are gone.
  $('btn-clear').addEventListener('click', () => {
    box.innerHTML = '<div class="log-empty">Cleared. New lines appear here as they arrive.</div>';
    total = 0;
    shown = 0;
    seenSources.forEach((id) => { sourceCounts[id] = 0; });
    renderSourceChips();
    syncCounts();
    showToast('Cleared the view. Nothing on disk was deleted.');
  });

  // ── Copy ────────────────────────────────────────────────────────────────

  // Copies only the rows the active filters leave visible - copying hidden rows would hand
  // back something other than what's on screen.
  $('btn-copy').addEventListener('click', async () => {
    const rows = [...box.querySelectorAll('.log-row')].filter((r) => !r.hidden);
    if (!rows.length) { showToast('Nothing to copy.', { type: 'error' }); return; }
    const text = rows.map((r) => [...r.children].map((c) => c.textContent).join('  ')).join('\n');
    try {
      await navigator.clipboard.writeText(text);
      showToast(`Copied ${rows.length.toLocaleString()} log line${rows.length === 1 ? '' : 's'}.`);
    } catch (e) {
      showToast('Copy failed - clipboard access was denied.', { type: 'error' });
    }
  });

  // ── Filter controls ─────────────────────────────────────────────────────

  srcChips.addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    const id = chip.dataset.src;
    if (activeSources.has(id)) activeSources.delete(id); else activeSources.add(id);
    applyFilters();
  });

  lvChips.addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    const lv = chip.dataset.level;
    if (activeLevels.has(lv)) activeLevels.delete(lv); else activeLevels.add(lv);
    applyFilters();
  });

  function setSearch(value) {
    searchText = value.toLowerCase().trim();
    searchWrap.classList.toggle('has-text', !!value);
    applyFilters();
  }

  let searchTimer;
  searchEl.addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    const { value } = e.target;
    searchTimer = setTimeout(() => setSearch(value), 150);
  });

  $('log-search-clear').addEventListener('click', () => {
    searchEl.value = '';
    clearTimeout(searchTimer);
    setSearch('');
    searchEl.focus();
  });

  // ── The phone sheet (DESIGN.md 16.5 item 4) ─────────────────────────────
  // The toolbar node is MOVED into the sheet and moved back on close, rather
  // than re-rendered inside it. One set of chips, one set of listeners, and no
  // way for a phone copy and a desktop copy to disagree about what is filtered.
  const filterSlot = $('log-filter-slot');
  const filters = $('log-filters');

  $('logfilter-btn').addEventListener('click', () => {
    if (sheet) { sheet.closeModal(); return; }
    const note = document.createElement('div');
    note.className = 'lf-note';
    note.textContent = 'Three sources are off by default because they are noisy. '
      + 'The count on a chip is how many of the loaded lines came from it.';
    filters.appendChild(note);
    sheet = buildModal({
      title: 'Filter',
      body: filters,
      footer: [
        {
          label: 'Reset',
          class: 'btn btn-secondary',
          onClick: () => {
            activeLevels.clear();
            LEVELS.forEach((l) => activeLevels.add(l.id));
            activeSources.clear();
            seenSources.forEach((id) => {
              const known = SOURCE_MAP[id];
              if (!known || !known.hidden) activeSources.add(id);
            });
            searchEl.value = '';
            setSearch('');
            return false;
          },
        },
        { label: 'Done', class: 'btn btn-primary' },
      ],
      onClose: () => {
        note.remove();
        // buildModal has already detached the overlay by the time this runs, so
        // the toolbar has to be put back or the page loses its only filter.
        filterSlot.appendChild(filters);
        sheet = null;
      },
    });
  });

  // ── History load ────────────────────────────────────────────────────────

  async function loadHistory() {
    try {
      const resp = await fetch('/api/logs/history?tail=1000');
      if (!resp.ok) throw new Error(resp.status);
      const records = await resp.json();
      if (!records.length) {
        box.innerHTML = '<div class="log-empty">No log history available.</div>';
        return;
      }
      records.forEach(appendRow);
      box.scrollTop = box.scrollHeight;
    } catch (e) {
      box.innerHTML = '<div class="log-empty">Could not load the log history. '
        + 'The live stream below will still show new lines.</div>';
      showToast(`Could not load the log history: ${e.message}`, { type: 'error' });
    }
  }

  // ── Boot ────────────────────────────────────────────────────────────────

  renderSourceChips();
  renderLevelChips();
  syncCounts();
  loadHistory().then(startStream);
})();
