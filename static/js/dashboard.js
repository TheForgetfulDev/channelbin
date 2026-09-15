/* Live Dashboard - DESIGN.md section 16 (approved 2026-08-03).
   Reference mockups dev/mockups/29-ops-desktop.html + 30-ops-mobile.html;
   rollout dev/changelog/445.

   WHAT THIS FILE DOES NOT DO: compose the page. templates/dashboard.html is the one
   composer for #dash-sections (16.6). This file owns three things and nothing else -
   the timeline (which has no server-rendered counterpart, because every bar is
   positioned in pixels against a scale only the client knows the viewport for),
   the Customize modal (which MOVES and HIDES the server's .dash-sec nodes rather
   than rebuilding them), and the SSE cell patches this page has always done.

   escHtml/fmtDur/fmtBytes/connectSSE/buildModal/jsonFetch and the time-axis scale
   constants all come from util.js, loaded in base.html <head>. */

'use strict';

const DASH = '-';

function setEl(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function readJson(id) {
  const el = document.getElementById(id);
  if (!el) return null;
  try { return JSON.parse(el.textContent); } catch (err) { return null; }
}

// ── SSE: recording cells ─────────────────────────────────────────────────────
// Carried over unchanged in structure. Only the cell ids it writes changed, because
// 16's row shows three stats where the old card showed seven; setEl on a missing id
// is a no-op, so a status whose row does not render a given stat simply skips it.

function handleSnapshot(rid, d) {
  setEl(`elapsed-${rid}`,   fmtDur(d.elapsed_seconds));
  setEl(`remaining-${rid}`, fmtDur(d.remaining_seconds));
  setEl(`bytes-${rid}`,     fmtBytes(d.total_bytes));

  // Stalls has no column of its own on a row, so its warning rides on the byte count
  // it explains - the same rule the server-rendered markup follows. Re-applied here
  // because a live row can cross the threshold without the page reloading.
  const bytesCell = document.getElementById(`bytes-cell-${rid}`);
  if (bytesCell && d.stall_count != null) bytesCell.classList.toggle('warn', d.stall_count > 5);

  const row = document.getElementById(`card-${rid}`);
  if (row && d.elapsed_seconds != null && d.remaining_seconds != null) {
    const total = d.elapsed_seconds + d.remaining_seconds;
    const pct = total > 0 ? Math.min(100, (d.elapsed_seconds / total) * 100) : 0;
    row.style.setProperty('--pct', `${pct.toFixed(1)}%`);
  }
  // Keeps a click-to-sort by Elapsed/Remaining/Recorded reading CURRENT numbers rather
  // than the values the page happened to render at, since this cell ticks every second
  // and the sort only re-reads it on click.
  if (row) {
    if (d.elapsed_seconds != null) row.dataset.elapsed = d.elapsed_seconds;
    if (d.remaining_seconds != null) row.dataset.remaining = d.remaining_seconds;
    if (d.total_bytes != null) row.dataset.recorded = d.total_bytes;
  }
}

function handleConversionProgress(rid, d) {
  if (d.out_size != null) setEl(`conv-bytes-${rid}`, fmtBytes(d.out_size));
  setEl(`conv-pct-${rid}`, d.pct != null ? `${Math.round(d.pct)}%` : DASH);
  setEl(`conv-eta-${rid}`, d.eta_seconds != null ? `~${fmtDur(d.eta_seconds)} left` : 'estimating...');
  const row = document.getElementById(`card-${rid}`);
  if (row && d.pct != null) row.style.setProperty('--pct', `${d.pct.toFixed(1)}%`);
  // Same freshness rule as handleSnapshot, for a converting row's Complete/Time
  // left/Written slot in those same three sort columns.
  if (row) {
    if (d.pct != null) row.dataset.elapsed = d.pct;
    if (d.eta_seconds != null) row.dataset.remaining = d.eta_seconds;
    if (d.out_size != null) row.dataset.recorded = d.out_size;
  }
}

function handleEvent(rid, eventType, d) {
  if (d && d.stall_count !== undefined) {
    const cell = document.getElementById(`bytes-cell-${rid}`);
    if (cell) cell.classList.toggle('warn', d.stall_count > 5);
  }
}

// The server's own status -> label table (app/fmt_utils.py), rendered into the page. Read
// once: nothing republishes it, and a miss falls back to the raw status so an unrecognized
// value is shown rather than swallowed - same contract as rec_status_display's default.
const STATUS_LABELS = readJson('dash-status-labels') || {};

function handleTerminal(rid, status) {
  const badge = document.getElementById(`badge-${rid}`);
  if (badge) {
    badge.className = `badge badge-${status.toLowerCase()}`;
    badge.textContent = STATUS_LABELS[status] || status;
  }
  const row = document.getElementById(`card-${rid}`);
  if (row && status === 'COMPLETED') row.style.setProperty('--pct', '100%');
  // Reload after a short delay to show final state.
  setTimeout(() => location.reload(), 3000);
}

function connect() {
  const statusEl = document.getElementById('sse-status');
  const setStatus = (cls, html, tip) => {
    if (!statusEl) return;
    statusEl.className = `badge ${cls}`;
    statusEl.innerHTML = html;
    statusEl.setAttribute('data-tip', tip);
  };
  connectSSE('/api/stream', {
    onOpen() {
      setStatus('badge-success', '<span class="pulse"></span>Live',
        'This page is receiving live activity updates over SSE.');
    },
    onMessage(e) {
      let msg;
      try { msg = JSON.parse(e.data); } catch (err) { return; }
      const rid = msg.recording_id;
      const evt = msg.event;
      const d   = msg.data || {};

      if (!document.getElementById(`card-${rid}`)) return;

      if (evt === 'STATS_SNAPSHOT') {
        handleSnapshot(rid, d);
      } else if (evt === 'CONVERSION_PROGRESS') {
        handleConversionProgress(rid, d);
      } else {
        handleEvent(rid, evt, d);
      }

      if (['COMPLETED', 'FAILED', 'ABORTED', 'CONCATENATION_DONE', 'CONVERSION_DONE',
           'RECORDING_FAILED', 'RECORDING_PAUSED', 'RECORDING_RETRY_SCHEDULED'].includes(evt)) {
        handleTerminal(rid, d.status || evt);
      }
    },
    onError() {
      setStatus('badge-warning', 'Reconnecting',
        'The activity stream dropped. Numbers below may be stale until it reconnects.');
    },
  });
}

// ── Channel health check card (polling, not SSE - channel_tester has no pub/sub) ──

let _hcRunStartedAt = null;

function pollHealthCheck() {
  fetch('/api/channel-tests/active-run')
    .then(r => r.json())
    .then(data => {
      if (!data || data.active === false) {
        const badge = document.getElementById('hc-status-badge');
        if (badge) { badge.textContent = 'FINISHED'; badge.className = 'badge badge-completed'; }
        setTimeout(() => location.reload(), 3000);
        return;
      }

      setEl('hc-progress-text', `${data.completed_channels} / ${data.total_channels}`);
      const row = document.getElementById('hc-card');
      if (row) {
        const pct = data.total_channels > 0
          ? Math.min(100, (data.completed_channels / data.total_channels) * 100) : 0;
        row.style.setProperty('--pct', `${pct.toFixed(1)}%`);
      }
      setEl('hc-results', `${data.pass_count}P ${data.warn_count}W ${data.fail_count}F`);
      _hcRunStartedAt = data.run_started_at;

      setTimeout(pollHealthCheck, 3000);
    })
    .catch(() => setTimeout(pollHealthCheck, 5000));
}

function startHealthCheckWidget() {
  if (!document.getElementById('hc-card')) return;

  setInterval(() => {
    if (!_hcRunStartedAt) return;
    const elapsed = (Date.now() - new Date(_hcRunStartedAt + 'Z').getTime()) / 1000;
    setEl('hc-elapsed', fmtDur(elapsed));
  }, 1000);

  pollHealthCheck();
}

/* ═══════════════════════════════════════════════════════════════════════════
   THE TIMELINE  (DESIGN.md 16.3 / 16.4)

   A horizontal SCROLLER, not a window fitted to the viewport. Rounds 1-8 of the
   design fitted it, and paid the trade in reach: at six hours neither nightly
   maintenance job, nor the 3:30am config backup, nor tomorrow morning's recording
   was on the axis at all. Pick a scale, pick a window, and the width falls out.
   ═══════════════════════════════════════════════════════════════════════════ */

const MIN = 60000;
const HOUR = 60 * MIN;

// Two hours back and twelve forward, the SAME window at both breakpoints. Back: far
// enough that a capture which finished recently and is still converting is ON the
// axis rather than missing from it - that row is on this page, so an axis that
// cannot show it is lying by omission. Forward: far enough that every recurring job
// the app schedules lands on it. The two surfaces differ only in how much is on
// screen at once, which is the thing that actually differs between a phone and a
// monitor (16.3).
const TL_BACK_HOURS = 2;
const TL_FWD_HOURS = 12;

// A job is drawn as a LENGTH when its estimated runtime is wide enough to read and
// as a POINT when it is not - never inflated to a legible bar that lies about its
// duration. In pixels rather than percent because with a scroller the window's width
// is no longer what decides legibility; the scale is.
const JOB_BAR_MIN_PX = 14;

const TL = {
  data: null, start: 0, end: 0, width: 0, pxPerMin: 0,
  // Scroll position is state held OUTSIDE the DOM, so every rebuild of the region
  // has to restore it (16.3, and the frontend rule that a rebuild re-applies every
  // piece of active state). null means "not scrolled yet" - the first render lands
  // on NOW rather than at the start of the window, because an axis that opens two
  // hours in the past is an axis whose first screen is already spent.
  scrollLeft: null,
};

const isPhone = () => window.matchMedia('(max-width: 768px)').matches;
const tlPx = t => (t - TL.start) / MIN * TL.pxPerMin;
// Through util.js, so the timeline reads the configured display timezone and clock format.
// This used to be a bare toLocaleTimeString([], ...) with neither, which rendered the
// BROWSER's timezone - five hours off from the guide-derived widgets on this same page for
// anyone whose machine disagreed with the setting (dev/docs/BUGS.md 2026-08-14).
const fmtClock = ms => fmtTimeTz(new Date(ms));

function fmtUntil(ms) {
  const d = ms - Date.now();
  if (d <= 0) return 'now';
  return `in ${fmtDur(d / 1000, false)}`;
}

// A recording is drawn where it was RECORDED, not where it is converting: that is the
// slot it occupied on the schedule.
const recWindow = r => [r.start, r.end];
// A job's window is its start plus its ESTIMATED runtime, so a job with no history
// has a zero-length one. Never widened to a minimum here - the minimum is a drawing
// concern and belongs in jobMark(), not in the arithmetic that decides whether this
// job actually collides with a recording.
const jobWindow = j => [j.start, j.start + (j.est_seconds || 0) * 1000];

/* Why a job is worth flagging, as prose, or null.

   16.4: the timeline exists to surface a decision the app already makes in the dark.
   scheduler.py yields a scheduled account sync when a recording is in progress, or when
   one starts within skip_sync_if_recording_within_minutes - and today the user learns
   this afterwards, if at all. The wording mirrors accounts.py's sync_conflicts, which
   already names the conflict AND its consequence.

   "Deferred", not "skipped": since dev/changelog/941 the occurrence is not dropped - the
   scheduler queues one retry at the first gap long enough to finish the sync in, which
   appears on this same timeline as its own account_sync_retry_<id> mark. A tooltip that
   still threatened a skip would describe a behavior the backend no longer has. */
function jobClash(job, recs, guards) {
  // Only a job that has not fired yet. Both guards are evaluated by the scheduler AT
  // FIRE TIME, so a run already under way cannot be deferred by them - saying otherwise
  // would put a warning on the axis that the code behind it would never produce.
  if (!job.is_sync || job.start <= TL.data.now) return null;
  const [js] = jobWindow(job);
  const guardMs = guards.skipWithinMinutes * MIN;
  for (const r of recs) {
    const [rs, re] = recWindow(r);
    if (guards.skipIfRecordingActive && r.status === 'IN_PROGRESS' && js >= rs && js < re) {
      return `Will be deferred: "${r.name}" is recording, and syncing now would count `
           + `against this account's connection limit. It retries at the first gap long `
           + `enough to finish in.`;
    }
    // Measured from the recording's START, not from its estimated finish, because
    // that is what scheduler.py compares against.
    if (r.status === 'SCHEDULED' && rs >= js && rs - js <= guardMs) {
      return `Will be deferred: "${r.name}" starts within ${guards.skipWithinMinutes} `
           + `minutes of it, and a sync that late risks the recording's first segment. `
           + `It retries at the first gap long enough to finish in.`;
    }
  }
  return null;
}

// Every status the timeline can be handed, named explicitly. A trailing `: 'converting'`
// used to absorb whatever was left, so PAUSED and RETRYING drew as blue conversion bars
// the moment they reached this lane (dev/changelog/663). An unmapped status falls to
// `unknown`, which is visibly not any real state rather than impersonating one.
const TL_BAR_CLASS = {
  IN_PROGRESS: 'live',
  PAUSED: 'stalled',
  RETRYING: 'stalled',
  CONCATENATING: 'converting',
  ANALYZING: 'converting',
  CONVERTING: 'converting',
  SCHEDULED: 'scheduled',
};

function tlBar(r, now) {
  const [s, e] = recWindow(r);
  const l = Math.max(0, tlPx(s));
  const rt = Math.min(TL.width, tlPx(e));
  const cls = TL_BAR_CLASS[r.status] || 'unknown';
  // The fill is the captured share of THIS bar, so it is a percentage of the bar's own
  // visible width, not of the window.
  let fill = '';
  if (r.status === 'IN_PROGRESS' && e > s && rt > l) {
    const done = (now - s) / (e - s) * 100;
    const visible = (rt - l) / (tlPx(e) - tlPx(s));
    fill = `<span class="tl-fill" style="width:${Math.max(0, Math.min(100, done / visible)).toFixed(2)}%"></span>`;
  }
  const clip = (tlPx(s) < 0 ? ' clip-l' : '') + (tlPx(e) > TL.width ? ' clip-r' : '');
  const tip = `${r.name}\n${r.channel || ''}\n${fmtClock(s)} - ${fmtClock(e)}`;
  return `<div class="tl-bar ${cls}${clip}" style="left:${l.toFixed(1)}px;width:${(rt - l).toFixed(1)}px"
    data-id="${r.id}" data-tip="${escHtml(tip)}" data-sheet="rec">${fill}
    <span class="tl-name">${escHtml(r.name)}</span>
    <span class="tl-ch">${escHtml(r.channel || '')}</span></div>`;
}

function jobDurationLine(j) {
  // 16.3: a job with no run history is drawn as an outline with NO LENGTH, because
  // the app has no estimate and inventing one puts a number on the axis that nothing
  // backs. Every job takes this branch today - runtime estimates do not exist yet.
  if (!j.est_seconds) return 'Expected runtime unknown - this job has no recorded run history.';
  return `Usually takes about ${fmtDur(j.est_seconds, false)} `
       + `(average of the last ${j.runs} run${j.runs === 1 ? '' : 's'}).`;
}

function jobMark(j, clash) {
  const [s, e] = jobWindow(j);
  const l = tlPx(s);
  const w = Math.min(TL.width, tlPx(e)) - Math.max(0, l);
  const bar = w >= JOB_BAR_MIN_PX;
  const cls = ['tl-job', bar ? 'bar' : '', clash ? 'clash' : '', !j.est_seconds ? 'unknown' : '']
    .filter(Boolean).join(' ');
  const pos = bar
    ? `left:${Math.max(0, l).toFixed(1)}px;width:${w.toFixed(1)}px`
    : `left:${Math.max(0, Math.min(TL.width, l)).toFixed(1)}px`;
  const tip = [j.name, `Next run ${fmtClock(s)} (${fmtUntil(s)})`, jobDurationLine(j), clash]
    .filter(Boolean).join('\n');
  return `<span class="${cls}" style="${pos}" data-job="${escHtml(j.id)}"
    data-tip="${escHtml(tip)}" data-sheet="job"><i></i></span>`;
}

function renderTimeline() {
  const host = document.getElementById('tl');
  if (!host || !TL.data) return;

  const now = TL.data.now;
  TL.pxPerMin = isPhone() ? PX_PER_MIN_MOBILE : PX_PER_MIN_DESKTOP;
  TL.start = now - TL_BACK_HOURS * HOUR;
  TL.end = now + TL_FWD_HOURS * HOUR;
  TL.width = Math.round((TL.end - TL.start) / MIN * TL.pxPerMin);

  // The timeline reads its data DIRECTLY, never through the section-visibility set:
  // hiding the Upcoming rows must not empty the axis (16.6).
  const recs = TL.data.recordings;
  const inWin = recs.filter(r => { const [s, e] = recWindow(r); return e > TL.start && s < TL.end; });
  const jobsIn = TL.data.jobs.filter(j => {
    const [s, e] = jobWindow(j);
    return e >= TL.start && s <= TL.end;
  });
  const outside = (recs.length - inWin.length) + (TL.data.jobs.length - jobsIn.length);

  // Greedy lane packing. Two recordings that overlap in time land in different lanes,
  // which is the whole point - an overlap is a real thing to see.
  const lanes = [];
  inWin.slice().sort((a, b) => a.start - b.start).forEach(r => {
    const [s, e] = recWindow(r);
    let lane = lanes.find(L => L.every(o => { const [os, oe] = recWindow(o); return oe <= s || os >= e; }));
    if (!lane) { lane = []; lanes.push(lane); }
    lane.push(r);
  });

  // One label per hour, all of them. A label centred within half its own width of an
  // end of the track would hang off it and be unreachable at either extreme of the
  // scroll, so those anchor to the edge instead - placement, not suppression.
  const ticks = [];
  const grid = [];
  const first = new Date(TL.start);
  first.setMinutes(0, 0, 0);
  for (let t = first.getTime() + HOUR; t < TL.end; t += HOUR) {
    const px = tlPx(t);
    grid.push(`<i style="left:${px.toFixed(1)}px"></i>`);
    const anchor = px < 30 ? ' first' : px > TL.width - 30 ? ' last' : '';
    ticks.push(`<span class="tl-tick${anchor}" style="left:${px.toFixed(1)}px">${escHtml(fmtClock(t))}</span>`);
  }

  const clashes = jobsIn.map(j => jobClash(j, inWin, TL.data.syncGuards));
  const nClash = clashes.filter(Boolean).length;
  const laneHtml = lanes.map(L => `<div class="tl-lane">${L.map(r => tlBar(r, now)).join('')}</div>`).join('')
    || '<div class="tl-lane"></div>';
  // One rail, not lanes: jobs are points at this scale, so packing them into lanes
  // would buy nothing and cost the eye a second row height to scan.
  const railHtml = jobsIn.length
    ? `<div class="tl-rail">${jobsIn.map((j, i) => jobMark(j, clashes[i])).join('')}</div>` : '';
  const nowLeft = tlPx(now).toFixed(1);
  const counts = `${inWin.length} recording${inWin.length === 1 ? '' : 's'}, `
               + `${jobsIn.length} job${jobsIn.length === 1 ? '' : 's'}`;

  // The axis row and the bars live inside ONE scroller, not two synced ones (16.3):
  // the guide needs two because its hour bar is sticky under a header; nothing here
  // is sticky, and one scroller cannot desync from itself. The legend and the count
  // line sit OUTSIDE it - a legend explains the whole drawing, so it must not be
  // something the reader can scroll away from.
  host.innerHTML = `
    <div class="tl-scroll" id="tl-scroll">
      <div class="tl-track" style="width:${TL.width}px">
        <div class="tl-axis">${ticks.join('')}<span class="tl-now lbl" style="left:${nowLeft}px"></span></div>
        <div class="tl-body">
          <div class="tl-grid">${grid.join('')}</div>
          <span class="tl-now" style="left:${nowLeft}px"></span>
          ${laneHtml}
          ${railHtml}
        </div>
      </div>
    </div>
    <button class="btn btn-sm tl-nowbtn" id="tl-nowbtn" type="button" hidden>Now</button>
    <div class="tl-foot">
      <span class="tl-key"><i class="live"></i>Recording</span>
      <span class="tl-key"><i class="converting"></i>Converting</span>
      <span class="tl-key"><i class="scheduled"></i>Scheduled</span>
      <span class="tl-key"><i class="job"></i>Job</span>
      ${nClash ? '<span class="tl-key clash"><i class="jobclash"></i>Job that will be skipped</span>' : ''}
      <span class="tl-off">${escHtml(counts)} in this window${outside ? `, ${outside} outside it` : ''}</span>
    </div>`;

  wireScroller();
}

// NOW a third of the way in, which is the guide's scrollToNow() rule exactly, from the
// same shared constant: enough past visible to see what just finished, most of the
// width spent on what has not happened yet.
const tlNowScroll = view => Math.max(0, tlPx(TL.data.now) - view / NOW_SCROLL_DIVISOR);

function wireScroller() {
  const sc = document.getElementById('tl-scroll');
  const btn = document.getElementById('tl-nowbtn');
  if (!sc || !btn) return;
  // One writer for the button's visibility and the stored position, so the button
  // cannot claim NOW is off-screen while it is sitting in the middle of the card.
  const sync = () => {
    // Off-screen means off-screen with a margin, so the button does not flicker in and
    // out while NOW sits right on the edge.
    const x = tlPx(TL.data.now) - sc.scrollLeft;
    btn.hidden = x > 24 && x < sc.clientWidth - 24;
  };
  sc.scrollLeft = TL.scrollLeft === null ? tlNowScroll(sc.clientWidth) : TL.scrollLeft;
  sc.addEventListener('scroll', () => { TL.scrollLeft = sc.scrollLeft; sync(); });
  btn.addEventListener('click', () => {
    TL.scrollLeft = tlNowScroll(sc.clientWidth);
    sc.scrollTo({ left: TL.scrollLeft, behavior: 'smooth' });
    sync();
  });
  sync();
}

/* A phone has no hover, so the desktop tooltip is not a design (16.5 item 1).
   Tapping a bar or a job marker opens a bottom sheet carrying exactly what the
   tooltip carries. The sheet is free: .modal-panel already becomes one at ≤768px,
   so this is buildModal(), not a second component. On desktop the hover tooltip
   already carries that same detail, so a click there goes straight to the item
   instead of showing a sheet only to make the reader tap Open a second time. */
document.addEventListener('click', e => {
  const el = e.target.closest('[data-sheet]');
  if (!el) return;
  const href = el.dataset.sheet === 'rec' ? `/recordings/${el.dataset.id}` : '/jobs';
  if (!isPhone()) { location.href = href; return; }
  const lines = (el.getAttribute('data-tip') || '').split('\n').filter(Boolean);
  if (!lines.length) return;
  buildModal({
    title: lines[0],
    body: lines.slice(1).map(l => `<p class="page-sub">${escHtml(l)}</p>`).join(''),
    footer: [
      { label: 'Close', class: 'btn' },
      { label: 'Open', class: 'btn btn-primary', onClick: () => { location.href = href; return false; } },
    ],
  });
});

/* ── Row/tile navigation - the same NO_NAV pattern index.html's recordings list
   and accounts.js already use (§17.5 item 6): the actions cell and anything
   tooltip-bearing are a row's dead zone, everything else navigates. .mtile has no
   interactive descendant to guard against - its own [data-tip] is the tile's own
   attribute, not a nested one, so it is handled as a direct click-through. ── */
const ROW_NO_NAV = '.c-actions, [data-tip]';
document.addEventListener('click', e => {
  const tile = e.target.closest('.mtile[data-href]');
  if (tile) { location.href = tile.dataset.href; return; }
  const row = e.target.closest('.drow[data-href]');
  if (row && !e.target.closest(ROW_NO_NAV)) location.href = row.dataset.href;
});

/* ── Sortable dash-head columns ───────────────────────────────────────────────
   Clicking a column header sorts that SECTION's own .dash-rows (live, upcoming and
   accounts each sort independently) via util.js's sortChildren, the same helper and
   pattern index.html's recordings list and group-detail.js's member table use.
   name/status compare as strings; every other column is a number staged on the row by
   the template (or kept live by the SSE handlers above), with a missing value sorting
   to the end regardless of direction - the group-detail.js member table's convention. */
const DASH_SORT_STRING = new Set(['name', 'status']);

function dashSortValue(row, key, dir) {
  const raw = row.dataset[key];
  if (DASH_SORT_STRING.has(key)) return raw || '';
  if (raw === undefined || raw === '') return dir === 'asc' ? Infinity : -Infinity;
  return parseFloat(raw);
}

function wireDashSort() {
  document.querySelectorAll('.dash-sec').forEach(sec => {
    const headers = Array.from(sec.querySelectorAll('.dash-head .sortable'));
    const rows = sec.querySelector('.dash-rows');
    if (!headers.length || !rows) return;
    let state = null;  // { key, dir } - unsorted (server order) until the first click
    const apply = () => {
      sortChildren(rows, '.drow', row => dashSortValue(row, state.key, state.dir), state.dir);
      headers.forEach(h => {
        const active = h.dataset.sort === state.key;
        h.classList.toggle('sorted', active);
        h.querySelector('.sort-ind').textContent = active ? (state.dir === 'desc' ? ' ▾' : ' ▴') : '';
      });
    };
    headers.forEach(h => {
      h.addEventListener('click', () => {
        const key = h.dataset.sort;
        state = (state && state.key === key)
          ? { key, dir: state.dir === 'asc' ? 'desc' : 'asc' }
          : { key, dir: DASH_SORT_STRING.has(key) ? 'asc' : 'desc' };
        apply();
      });
    });
  });
}

/* ═══════════════════════════════════════════════════════════════════════════
   CUSTOMIZE  (16.1: the section set and their order are user-controlled)

   This MOVES and HIDES the sections the server already rendered - it never rebuilds
   #dash-sections. That is what keeps the "one composer per region" rule honest and
   what stops a reorder from wiping every live SSE value on the page.
   ═══════════════════════════════════════════════════════════════════════════ */

const SEC = { key: '', order: [], on: {}, defs: {} };

function applySections() {
  const host = document.getElementById('dash-sections');
  const none = document.getElementById('dash-none');
  if (!host) return;
  SEC.order.forEach(k => {
    const el = host.querySelector(`.dash-sec[data-sec="${k}"]`);
    if (!el) return;
    el.hidden = !SEC.on[k];
    // Re-appending in order is a move, not a re-render: the nodes and their live
    // values are the same objects.
    host.insertBefore(el, none);
  });
  if (none) none.hidden = SEC.order.some(k => SEC.on[k]);
}

function saveSections() {
  jsonFetch(`/api/user-prefs/${SEC.key}`, {
    method: 'POST',
    body: JSON.stringify({ value: { order: SEC.order, on: SEC.on } }),
  }).catch(() => {});
}

function customizeBody() {
  return SEC.order.map((k, i) => {
    const d = SEC.defs[k];
    return `<div class="cz-row${SEC.on[k] ? '' : ' off'}" data-k="${k}">
      <label class="switch"><input type="checkbox" data-cz-on="${k}"${SEC.on[k] ? ' checked' : ''}><span class="knob"></span></label>
      <span class="cz-name">${escHtml(d.label)}<small>${escHtml(d.hint)}</small></span>
      <span class="cz-move">
        <button type="button" data-cz-up="${k}"${i === 0 ? ' disabled' : ''} aria-label="Move up">&uarr;</button>
        <button type="button" data-cz-down="${k}"${i === SEC.order.length - 1 ? ' disabled' : ''} aria-label="Move down">&darr;</button>
      </span></div>`;
  }).join('');
}

let _czModal = null;

function refreshCustomize() {
  if (!_czModal) return;
  const body = _czModal.querySelector('.modal-body');
  if (body) body.innerHTML = customizeBody();
}

// Delegated, because the modal body is rebuilt on every change - per-element handlers
// would be lost the moment a row moved.
document.addEventListener('click', e => {
  const up = e.target.closest('[data-cz-up]');
  const down = e.target.closest('[data-cz-down]');
  const k = up ? up.dataset.czUp : down ? down.dataset.czDown : null;
  if (!k) return;
  const i = SEC.order.indexOf(k);
  const j = up ? i - 1 : i + 1;
  if (j < 0 || j >= SEC.order.length) return;
  [SEC.order[i], SEC.order[j]] = [SEC.order[j], SEC.order[i]];
  refreshCustomize();
  applySections();
  saveSections();
  showToast(`Moved ${SEC.defs[k].label} ${up ? 'up' : 'down'}.`);
});

document.addEventListener('change', e => {
  const t = e.target.closest('[data-cz-on]');
  if (!t) return;
  const k = t.dataset.czOn;
  SEC.on[k] = !SEC.on[k];
  refreshCustomize();
  applySections();
  saveSections();
  showToast(`${SEC.defs[k].label} ${SEC.on[k] ? 'shown' : 'hidden'}.`);
});

function initDashboard() {
  const prefs = readJson('dash-sections-pref');
  if (prefs) {
    SEC.key = prefs.key;
    SEC.order = prefs.order;
    SEC.on = prefs.on;
    SEC.defs = prefs.defs;
    const btn = document.getElementById('btn-customize');
    if (btn) {
      btn.addEventListener('click', () => {
        _czModal = buildModal({
          title: 'Customize this page',
          body: customizeBody(),
          footer: [{ label: 'Done', class: 'btn' }],
          footNote: 'Saved against your user preferences, so it follows you between browsers.',
          onClose: () => { _czModal = null; },
        });
      });
    }
  }

  TL.data = readJson('dash-timeline');
  renderTimeline();
  // The scale is a property of the breakpoint, and the breakpoint can change under a
  // live page. Re-rendering restores the saved scroll position by way of TL.scrollLeft.
  window.addEventListener('resize', renderTimeline);

  wireDashSort();
  connect();
  startHealthCheckWidget();
}

document.addEventListener('DOMContentLoaded', initDashboard);
