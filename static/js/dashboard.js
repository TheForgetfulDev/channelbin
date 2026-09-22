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
  // A parked row renders no conv-eta element at all - the template puts "Waiting on" in
  // that cell instead, because an ETA measured before the encoder was stopped is a clock
  // that has stopped (dev/changelog/1053). setEl no-ops on the missing id, which is the
  // point: the absence is the guard, so there is no flag here to get wrong. Park and
  // unpark both arrive as status frames, and handleStatus reloads the page for them.
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

let _reloadTimer = null;

// Keyed on the `status` a frame carries, never on its event name: every lifecycle publish
// in app/ sends one, and a hand-kept list of names drifted until it matched three events
// nothing sent and missed six that moved a row (dev/changelog/1023). A frame without a
// known status - most of them - says nothing about the badge and leaves it alone.
//
// Acts only on a CHANGE from what the row shows: CONVERSION_PROGRESS and STATS_SNAPSHOT
// repeat the current status every tick, and each change schedules a reload so the row's
// server-rendered cells catch up with its new phase.
// The word comes from util.js's recStatusLabel - the server's own table (app/fmt_utils.py),
// served to every page by base.html. A status the table does not name is not a status this
// page can relabel, so the frame is left alone rather than badged from its raw enum.
function handleStatus(rid, d) {
  const label = recStatusLabel(d.status);
  if (label === null) return;
  const row = document.getElementById(`card-${rid}`);
  if (!row) return;
  const wasWaiting = row.dataset.waiting === '1';
  // Only the yield/resume frames carry `waiting`. Any other frame keeps the parked flag
  // within one status and drops it across a status change, as the server's stamp does.
  let waiting;
  if (typeof d.waiting === 'boolean') waiting = d.waiting;
  else waiting = d.status === row.dataset.status ? wasWaiting : false;
  if (d.status === row.dataset.status && waiting === wasWaiting) return;

  row.dataset.status = d.status;
  row.dataset.waiting = waiting ? '1' : '';
  const badge = document.getElementById(`badge-${rid}`);
  if (badge) {
    badge.className = `badge badge-${d.status.toLowerCase()}`;
    const parked = recWaitingLabel();
    badge.textContent = (waiting && parked) ? parked : label;
  }
  if (d.status === 'COMPLETED') row.style.setProperty('--pct', '100%');
  if (_reloadTimer === null) _reloadTimer = setTimeout(() => location.reload(), 3000);
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

      handleStatus(rid, d);
    },
    onError() {
      setStatus('badge-warning', 'Reconnecting',
        'The activity stream dropped. Numbers below may be stale until it reconnects.');
    },
  });
}

/* ── Channel health check card (polling, not SSE - channel_tester has no pub/sub) ──

   TWO WRITERS, ON TWO SCALES, AND THEY DO NOT OVERLAP. The 3s poll below writes CELLS
   INSIDE the card by id and nothing else. The card itself - whether the section holds a
   row at all - is written only by refreshSection('health'), the same way the template
   owns a recording row while SSE patches its cells.

   The widget is startable more than once because the section it lives in is swapped out
   from under it: every start after the first has to find either its own live poll (and
   leave it alone) or a fresh card (and adopt it). Both timers are stoppable for the same
   reason - a poll left running against a card that has been replaced is the second writer
   the rule above exists to prevent, and it would keep asking the server forever. */

let _hcRunStartedAt = null;
let _hcPolling = false;
let _hcElapsedTimer = null;

function stopHealthCheckWidget() {
  _hcPolling = false;
  if (_hcElapsedTimer !== null) { clearInterval(_hcElapsedTimer); _hcElapsedTimer = null; }
  _hcRunStartedAt = null;
}

function pollHealthCheck() {
  // Checked before the request and again after it: a swap can land during the round trip,
  // and a chain that outlives its own card must not schedule another turn.
  if (!_hcPolling || !document.getElementById('hc-card')) { stopHealthCheckWidget(); return; }
  fetch('/api/channel-tests/active-run')
    .then(r => r.json())
    .then(data => {
      if (!_hcPolling || !document.getElementById('hc-card')) { stopHealthCheckWidget(); return; }
      if (!data || data.active === false) {
        // The run is over. The badge says so immediately - the poll is what learns this
        // first, ~15s before the nav-status hook could - and then the section is swapped
        // for its server render, which is the empty state. This used to reload the whole
        // page instead, which threw away the timeline's scroll position, every section's
        // sort and the SSE connection to whatever was still recording, to refresh one
        // row (dev/changelog/1082).
        const badge = document.getElementById('hc-status-badge');
        if (badge) { badge.textContent = 'FINISHED'; badge.className = 'badge badge-completed'; }
        stopHealthCheckWidget();
        refreshSection('health');
        return;
      }

      // A DIFFERENT run than the one this card was drawn for: the previous one finished
      // and another started inside a single turn of this poll, so the nav hook saw a
      // health check both times and had nothing to react to. Without this the cells below
      // would keep counting under the finished run's name and link - the reload this
      // replaced could not hit it, because it rebuilt the whole page (dev/changelog/1082).
      // The live chain keeps running and adopts the swapped-in card by id.
      if (_hcRunStartedAt && data.run_started_at !== _hcRunStartedAt) refreshSection('health');

      setEl('hc-progress-text', `${data.completed_channels} / ${data.total_channels}`);
      const row = document.getElementById('hc-card');
      if (row) {
        const pct = data.total_channels > 0
          ? Math.min(100, (data.completed_channels / data.total_channels) * 100) : 0;
        row.style.setProperty('--pct', `${pct.toFixed(1)}%`);
      }
      setEl('hc-results', `${data.pass_count}P ${data.warn_count}W ${data.fail_count}F`);
      // A magnitude, not a countdown - the server re-estimates about once per channel, so
      // this holds still between samples while the elapsed ticker above it keeps moving.
      // Blank until a channel has finished, because until then nothing has been measured.
      setEl('hc-eta', data.eta_seconds != null ? `~${fmtDur(data.eta_seconds, false)} left` : '');
      _hcRunStartedAt = data.run_started_at;

      setTimeout(pollHealthCheck, 3000);
    })
    .catch(() => setTimeout(pollHealthCheck, 5000));
}

function startHealthCheckWidget() {
  if (!document.getElementById('hc-card')) { stopHealthCheckWidget(); return; }
  // Already polling: the card was swapped for another card (a second run started, or the
  // section simply re-rendered), and the live chain adopts it by id on its next turn.
  // Starting a second one here is how the card ends up with two writers.
  if (_hcPolling) return;

  _hcElapsedTimer = setInterval(() => {
    if (!_hcRunStartedAt) return;
    const elapsed = (Date.now() - new Date(_hcRunStartedAt + 'Z').getTime()) / 1000;
    setEl('hc-elapsed', fmtDur(elapsed));
  }, 1000);

  _hcPolling = true;
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
  // The clock the axis is drawn against, as two readings taken at the same instant:
  // the server's `now` from the page blob, and what this browser's clock said when the
  // blob was read. advanceNow() adds the ELAPSED browser time to the server's reading
  // rather than taking Date.now() outright, because every other time on this axis - a
  // bar's start and end, a job's next run - is the server's. A machine whose clock is
  // ten minutes out would otherwise slide the whole drawing ten minutes sideways at the
  // first tick and keep it there, and the row counters beside it are server-timed too.
  serverNow: 0, readAt: 0,
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
      ${nClash ? '<span class="tl-key clash"><i class="jobclash"></i>Job that will be deferred</span>' : ''}
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

/* ── The axis keeps time ──────────────────────────────────────────────────────
   Everything the timeline draws is positioned against TL.data.now: the hour ticks,
   every bar's left edge, and the captured share of a live recording's bar. Without
   a tick that number stays at the server's render time forever, so the whole card
   is a photograph taken at page load while the row beneath it counts up every
   second from SSE - an hour on a dashboard left open is an hour of the axis being
   wrong with nothing saying so.

   The now-line itself does NOT move, and that is correct rather than a symptom:
   TL.start is always now - TL_BACK_HOURS, so tlPx(now) is a constant. NOW is a
   fixed post and the drawing slides leftward underneath it, which is also why the
   restored scroll offset in wireScroller keeps it at the same place on screen.

   renderTimeline stays the ONE writer of #tl (the one-updater-per-region rule) -
   the timer, the visibility handler and the resize listener all call it and none
   of them touches the region itself. */
function advanceNow() {
  if (!TL.data) return;
  TL.data.now = TL.serverNow + (Date.now() - TL.readAt);
}

function startTimelineClock() {
  const tick = () => { advanceNow(); renderTimeline(); };
  setInterval(() => { if (!document.hidden) tick(); }, MIN);
  // A hidden tab skips its ticks, so it owes a render the moment it is looked at
  // again - otherwise coming back to a backgrounded dashboard shows an axis up to a
  // minute stale, which is the same defect in miniature.
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
}

/* A phone has no hover, so the desktop tooltip is not a design (16.5 item 1).
   Tapping a bar or a job marker opens a bottom sheet carrying exactly what the
   tooltip carries. The sheet is free: .modal-panel already becomes one at ≤768px,
   so this is buildModal(), not a second component. On desktop the hover tooltip
   already carries that same detail, so a click there goes straight to the item
   instead of showing a sheet only to make the reader tap Open a second time. */
const sheetHref = el => (el.dataset.sheet === 'rec' ? `/recordings/${el.dataset.id}` : '/jobs');
bindNavClicks(document, e => {
  const el = e.target.closest('[data-sheet]');
  return el && !isPhone() ? sheetHref(el) : null;
});
document.addEventListener('click', e => {
  const el = e.target.closest('[data-sheet]');
  if (!el || !isPhone()) return;
  const href = sheetHref(el);
  const lines = (el.getAttribute('data-tip') || '').split('\n').filter(Boolean);
  if (!lines.length) return;
  buildModal({
    title: lines[0],
    body: lines.slice(1).map(l => `<p class="page-sub">${escHtml(l)}</p>`).join(''),
    footer: [
      { label: 'Close', class: 'btn' },
      { label: 'Open', class: 'btn btn-primary', onClick: () => { location.href = href; return false; } },  // nav-ok: the phone sheet's Open button, a tap
    ],
  });
});

/* ── Row/tile navigation - the same NO_NAV pattern index.html's recordings list
   and accounts.js already use (§17.5 item 6): the actions cell and anything
   tooltip-bearing are a row's dead zone, everything else navigates. .mtile has no
   interactive descendant to guard against - its own [data-tip] is the tile's own
   attribute, not a nested one, so it is handled as a direct click-through. ── */
const ROW_NO_NAV = '.c-actions, [data-tip]';
bindNavClicks(document, e => {
  const tile = e.target.closest('.mtile[data-href]');
  if (tile) return tile.dataset.href;
  const row = e.target.closest('.drow[data-href]');
  return row && !e.target.closest(ROW_NO_NAV) ? row.dataset.href : null;
});

/* ── Sortable dash-head columns ───────────────────────────────────────────────
   Clicking a column header sorts that SECTION's own .dash-rows (live, upcoming and
   accounts each sort independently) via util.js's sortChildren, the same helper and
   pattern index.html's recordings list and group-detail.js's member table use.
   name/status compare as strings; every other column is a number staged on the row by
   the template (or kept live by the SSE handlers above), with a missing value sorting
   to the end regardless of direction - the group-detail.js member table's convention.

   The chosen sort is held per SECTION KEY rather than in each section's own closure,
   because two of these sections are swapped for a fresh server render while the page is
   open. A closure goes with the nodes it was bound to, so the user's sort would silently
   revert to server order on the first refresh - the frontend rule that a rebuild re-applies
   every piece of active state (dev/changelog/1082). */
const DASH_SORT_STRING = new Set(['name', 'status']);
const DASH_SORT = new Map();

function dashSortValue(row, key, dir) {
  const raw = row.dataset[key];
  if (DASH_SORT_STRING.has(key)) return raw || '';
  if (raw === undefined || raw === '') return dir === 'asc' ? Infinity : -Infinity;
  return parseFloat(raw);
}

function wireDashSec(sec) {
  const secKey = sec.dataset.sec;
  const headers = Array.from(sec.querySelectorAll('.dash-head .sortable'));
  const rows = sec.querySelector('.dash-rows');
  if (!headers.length || !rows) return;
  const apply = () => {
    const state = DASH_SORT.get(secKey);
    if (!state) return;  // unsorted - the server's order, which is a real state
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
      const state = DASH_SORT.get(secKey);
      DASH_SORT.set(secKey, (state && state.key === key)
        ? { key, dir: state.dir === 'asc' ? 'desc' : 'asc' }
        : { key, dir: DASH_SORT_STRING.has(key) ? 'asc' : 'desc' });
      apply();
    });
  });
  // A no-op on the first wire and the whole point of the second: a section swapped in
  // after the user sorted it arrives in server order and is re-sorted here.
  apply();
}

function wireDashSort() {
  document.querySelectorAll('.dash-sec').forEach(wireDashSec);
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

/* ═══════════════════════════════════════════════════════════════════════════
   SECTIONS THAT KEEP THEMSELVES CURRENT  (the 2026-08-11 call: polling for
   these two, SSE only for live recordings)

   Two of the five sections go stale on a page left open. The accounts rows say
   "Synced 4m ago" an hour later and never notice a sync starting, finishing or
   failing; the health section is rendered empty unless the page happened to load
   DURING a run, so a nightly check that starts while you are watching is invisible
   until a reload. Both already have a trigger on /api/nav-status and neither was
   reading it (dev/changelog/1001, 1082).

   ONE WRITER PER REGION, several triggers into it - the shape accounts.js uses. The
   writer is refreshSection(), which asks the server for a fresh copy of the section
   and swaps it in whole, so the template stays the only thing that knows what a row
   looks like. It never patches a cell from JSON, and nothing else replaces these
   nodes: Customize MOVES and HIDES them, the SSE handlers write cells in the two
   recording sections, and renderTimeline owns #tl.

   The baseline every trigger compares against is READ BACK OUT OF THE DOM - the
   signature the accounts section was rendered at, and whether a health card is on the
   page - rather than remembered from the first payload. The server-rendered markup is
   the ground truth about what the user is looking at, so there is no stored flag to
   drift out of step with it, and a run that starts in the gap between the render and
   the first poll is caught rather than missed.
   ═══════════════════════════════════════════════════════════════════════════ */

// The two sections with a live trigger. Everything else is load-once on purpose: the
// recording sections are SSE-driven, and the timeline redraws from its own clock.
const SELF_REFRESHING = new Set(['accounts', 'health']);
const ACCOUNTS_RERENDER_MS = 60 * 1000;

const _swapping = new Set();
const _sectionRenderedAt = new Map();

const dashSec = key => document.querySelector(`.dash-sec[data-sec="${key}"]`);
const healthCardPresent = () => Boolean(document.getElementById('hc-card'));

function refreshSection(key) {
  if (!SELF_REFRESHING.has(key) || _swapping.has(key) || !dashSec(key)) return;
  _swapping.add(key);
  swapFromServer([`.dash-sec[data-sec="${key}"]`])
    .then(() => {
      _sectionRenderedAt.set(key, Date.now());
      // Everything the swap threw away and the user chose: this section's sort, and the
      // order/visibility Customize holds outside the markup.
      const sec = dashSec(key);
      if (sec) wireDashSec(sec);
      applySections();
      // The fresh card (or the absence of one) decides whether the 3s poll runs; the
      // widget adopts a card it is already polling rather than doubling up on it.
      if (key === 'health') startHealthCheckWidget();
    })
    .catch(err => console.warn(`Dashboard ${key} section refresh failed; the next poll retries.`, err))
    .finally(() => _swapping.delete(key));
}

/* Two triggers, each covering what the other cannot - accounts.js's pair, for the same
   two reasons: the signature moves when a sync starts, ends or fails, and the minute tick
   is what stops "Synced 4m ago" and "in 23h 44m" drifting while nothing changes.

   The tick is also the answer for the row's health signal line (dev/changelog/1063), and
   that is worth stating rather than assuming: Avg score, Failing now and the band bar move
   with HEALTH CHECKS, not with syncs, so the sync signature is structurally the wrong
   trigger for them and only the tick covers them at all.

   Skipped while the tab is in the background or the section is hidden in Customize, since
   the tick is the only unconditional recurring cost here. Neither needs its own recovery:
   the next nav poll after the tab or the section comes back finds a render older than a
   minute and refreshes then. */
window.__applyAccountSync = (sig) => {
  const sec = dashSec('accounts');
  if (!sec || typeof sig !== 'string') return;
  if (sig !== sec.dataset.syncSig) { refreshSection('accounts'); return; }
  if (document.hidden || sec.hidden) return;
  const at = _sectionRenderedAt.get('accounts') || 0;
  if (Date.now() - at >= ACCOUNTS_RERENDER_MS) refreshSection('accounts');
};

// Which task row means "a health check is running", from the server's own vocabulary
// (app/routes/dashboard.py). Keyed on `kind` and never on `label`, which is display text
// and has been reworded twice on this payload already.
const BG_HEALTH_KIND = readJson('dash-bg-health-kind');

window.__applyBackgroundTasks = (bg) => {
  if (!BG_HEALTH_KIND || !dashSec('health')) return;
  const tasks = (bg && Array.isArray(bg.tasks)) ? bg.tasks : [];
  const running = tasks.some(t => t && t.kind === BG_HEALTH_KIND);
  // The section already shows the truth on both edges, which is also what keeps this
  // quiet on every steady poll and stops it racing the swap the 3s poll just asked for.
  if (running === healthCardPresent()) return;
  refreshSection('health');
};

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
  // Both readings together, before anything else can cost time between them.
  TL.serverNow = TL.data ? TL.data.now : 0;
  TL.readAt = Date.now();
  renderTimeline();
  // The scale is a property of the breakpoint, and the breakpoint can change under a
  // live page. Re-rendering restores the saved scroll position by way of TL.scrollLeft.
  window.addEventListener('resize', renderTimeline);
  startTimelineClock();

  wireDashSort();
  connect();
  startHealthCheckWidget();
  // The minute tick measures from the SERVER RENDER, not from the first swap, so a page
  // left open from the moment it loaded is refreshed a minute later like any other.
  SELF_REFRESHING.forEach(key => _sectionRenderedAt.set(key, Date.now()));
}

document.addEventListener('DOMContentLoaded', initDashboard);
