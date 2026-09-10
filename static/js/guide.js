'use strict';

// Time scale. PX_PER_MIN_DESKTOP / PX_PER_MIN_MOBILE and NOW_SCROLL_DIVISOR are declared in
// util.js, not here: the Live Dashboard's timeline draws a second time axis at the same
// scale, and DESIGN.md 16.3 requires one definition rather than a copy on each page.
// PIXELS_PER_MINUTE below stays a `let` because the scale is a property of the breakpoint,
// and the breakpoint can change under a live page (rotation, a resized window) - every
// reader takes it at call time.
const SLOT_MINUTES = 30;        // label every 30 min
const CUT_WIDTH_PX = 8;         // width of the "gap collapsed" cut divider

// The one breakpoint declaration. It must stay equal to the `@media (max-width: 768px)`
// query in guide.css - CSS decides what the grid LOOKS like and this decides what it is
// BUILT from, and the two disagreeing is a class of bug this file has shipped before.
const MOBILE_MQ = window.matchMedia('(max-width: 768px)');

// True at phone widths on any page that has opted in by sending a `layoutMobile` dict.
// The guide always does; channel detail's "What's On" card did not until its own revamp
// approved a mobile design for it (dev/changelog/345 explains the opt-out, 349 flips it).
// The test stays because the opt-in is what a page uses to say DESIGN.md 13's behaviours -
// 3px/min, the program sheet in place of the record modal, the bottom sheets - are ones it
// actually wants; a page embedding this grid without a mobile design must not get them
// silently.
function isMobileGuide() {
  return MOBILE_MQ.matches && !!GUIDE_CONFIG.layoutMobile;
}

// A page embedding the grid for ONE known channel (channel detail's What's On card). It
// draws no channel column and cannot sort or hide channels, so every control over those is
// omitted rather than rendered inert. Declared once here because both the row-height
// calculation and the mobile Layout sheet have to agree about it.
const SINGLE_CHANNEL = !!GUIDE_CONFIG.singleChannel;

let PIXELS_PER_MINUTE = isMobileGuide() ? PX_PER_MIN_MOBILE : PX_PER_MIN_DESKTOP;

// How many days the grid spans. Server-supplied from sync.epg_days_ahead, so the grid can
// never promise more time than the EPG importer stored - and so no visible string has to
// carry a typed day count (DESIGN.md 12.1). The literal is the fallback for pages that
// embed the grid without the setting (channel detail).
const WINDOW_DAYS = GUIDE_CONFIG.windowDays || 3;

// The display timezone, from util.js's page-wide source rather than a guide-specific
// config key - the grid is embedded on four pages and each one used to inject its own copy.
const TZ = displayTz();

let windowStart = null;  // Date (UTC) - start of rendered span
let windowEnd   = null;  // Date (UTC) - end of rendered span
let channelData = [];    // last API response
let fetchTimer  = null;

// ── Layout state (DESIGN.md 12.3) ────────────────────────────────────────────
// Server-side per-user state via /api/user-prefs, never localStorage (DESIGN.md 3.11).
// GUIDE_CONFIG.layout is the SAME dict the Layout popover's checkboxes were rendered from,
// so a checkbox's markup cannot drift from its JS default. These literals are only the
// fallback for pages that embed the grid without the popover (channel detail) - the guide
// page always overrides them, and app/routes/guide.py's GUIDE_LAYOUT_FIELD_DEFAULTS is the
// authority. Keep the two in step.
const LAYOUT_FALLBACK = {
  collapse_gaps: true, show_failed: false,
  rec_status: true, subtitle: true, description: true,
  start_time: false, end_time: false, duration: false,
  tag_dots: true,
  ch_resolution: true, ch_fps: true, ch_bitrate: true, ch_audio: true,
  sort_key: 'guide', sort_reversed: false, ch_logo_only: false,
};

// The program cell's orderable fields (13.9). Desktop renders them in 12.5's fixed order;
// mobile renders them in the user's stored order, which is why the order is state rather
// than a constant there. Must stay equal to GUIDE_CELL_FIELDS in app/routes/guide.py.
const CELL_FIELDS = ['rec_status', 'title', 'subtitle', 'time', 'tag_dots'];
const CELL_ORDER_DESKTOP = ['rec_status', 'title', 'subtitle', 'description', 'time', 'tag_dots'];
LAYOUT_FALLBACK.field_order = CELL_FIELDS.slice();

// Which breakpoint's stored Layout applies. Both dicts are rendered into the page because
// the breakpoint is a media query the server cannot see; pages that embed the grid without
// a Layout surface (channel detail) send neither and fall through to the literals above.
function activeLayoutSource() {
  if (isMobileGuide() && GUIDE_CONFIG.layoutMobile) return GUIDE_CONFIG.layoutMobile;
  return GUIDE_CONFIG.layout || {};
}

function activeLayoutPrefKey() {
  if (isMobileGuide() && GUIDE_CONFIG.layoutMobilePrefKey) return GUIDE_CONFIG.layoutMobilePrefKey;
  return GUIDE_CONFIG.layoutPrefKey;
}

// Every page that renders this grid now sends a full `layout` dict, so the literals above
// are a fallback and nothing else. collapse_gaps used to arrive separately from channel
// detail (GUIDE_CONFIG.collapseGapsDefault) because that page had no Layout surface; it has
// one now, and the server seeds the SAME field from display.guide_collapse_gaps for both
// pages inside read_guide_layout(), so the configured value still lands - through one path
// instead of two (BUGS.md 2026-07-26 11:04 PM, re-pointed in dev/changelog/349).
const LAYOUT = Object.assign({}, LAYOUT_FALLBACK, activeLayoutSource());

// LAYOUT is mutated in place by every control on the page, so crossing the breakpoint
// rewrites its contents rather than rebinding it - a rebind would leave every closure that
// captured the old object writing to a dict nothing reads.
function reloadLayoutForBreakpoint() {
  Object.keys(LAYOUT).forEach(k => { delete LAYOUT[k]; });
  Object.assign(LAYOUT, LAYOUT_FALLBACK, activeLayoutSource());
  sortKey = LAYOUT.sort_key;
  sortReversed = !!LAYOUT.sort_reversed;
}

let collapsedSegments = [];  // last computed {start, end, leftPx, widthPx} list, when collapse mode is active
let leadingCut = false;      // true when the first collapsed segment starts after windowStart

// ── Sort state (DESIGN.md 12.4 as amended 2026-07-20) ────────────────────────
// Keys and behaviour are shared verbatim with mobile (13.7); only the entry point differs.
// Each key declares its own natural direction, because direction resets to that default
// whenever the key changes - guide order ascending, name A-Z, health good-first.
const SORT_KEYS = [
  { key: 'guide',  label: 'Guide order',  natural: 'Normal',    reversed: 'Reversed' },
  { key: 'name',   label: 'Name',         natural: 'A-Z',       reversed: 'Z-A' },
  { key: 'health', label: 'Health score', natural: 'Good first', reversed: 'Bad first' },
];
// Seeded from the stored Layout pref, so the sort survives a reload on BOTH breakpoints
// (2026-07-26 - 13.6 requires it for the mobile Layout sheet, and desktop was ruled to
// behave the same rather than forgetting its sort). Only the entry point differs.
let sortKey = LAYOUT.sort_key;
let sortReversed = !!LAYOUT.sort_reversed;

// Per-line heights of everything a program cell can render, and of the channel column's two
// lines. These MUST match the line-height/height values in guide.css - they are what
// computeRowHeight() adds up to size rows to the tallest cell either side produces, so a
// cell can never clip (DESIGN.md 12.5). Change one and you must change the other.
const CELL_LINE = { rec: 13, title: 18, subtitle: 15, description: 28, meta: 14, gap: 1, padV: 8, offset: 7 };
const CHAN_LINE = { top: 30, tech: 15, gap: 3, padV: 8 };
// Mobile's mini-column stacks the logo over a two-line name instead (13.5). `logo` is the
// logo box, `name` the two clamped name lines - both must match .guide-channel-row's mobile
// rules in guide.css.
const CHAN_LINE_MOBILE = { logo: 28, name: 22, gap: 3, padV: 6 };

// ── Window management ────────────────────────────────────────────────────────

function initWindow() {
  const now = new Date();
  const ms = now.getTime();
  const halfHour = 30 * 60 * 1000;
  windowStart = new Date(Math.floor(ms / halfHour) * halfHour);
  windowEnd   = new Date(windowStart.getTime() + WINDOW_DAYS * 24 * 60 * 60 * 1000);
}

// ── Scroll helpers ────────────────────────────────────────────────────────────

function scrollGuide(minutes) {
  const area = document.getElementById('guide-program-area');
  if (area) area.scrollLeft += minutes * PIXELS_PER_MINUTE;
}

function scrollToNow() {
  const area = document.getElementById('guide-program-area');
  if (!area) return;
  const now = new Date();
  const offsetMin = (now - windowStart) / 60000;
  const nowPx = offsetMin * PIXELS_PER_MINUTE;
  const viewWidth = area.clientWidth;
  area.scrollLeft = Math.max(0, nowPx - viewWidth / NOW_SCROLL_DIVISOR);
}

// ── Layout popover + persistence (DESIGN.md 12.3) ────────────────────────────

function saveLayout() {
  const key = activeLayoutPrefKey();
  if (!key) return;
  jsonFetch(`/api/user-prefs/${key}`, {
    method: 'POST',
    body: JSON.stringify({ value: LAYOUT }),
  }).catch(() => {});
}

// Sort is Layout state now, so the two are written together rather than the sort control
// keeping a private copy that the stored dict never learns about.
function saveSortState() {
  LAYOUT.sort_key = sortKey;
  LAYOUT.sort_reversed = sortReversed;
  saveLayout();
}

// The health filter is a CSS class on the grid body, so the server-rendered markup already
// carries the user's stored choice and this only re-asserts it.
//
// Never applied to a single-channel embed. "Include failed channels" is off by default and
// that page offers no control to turn it on, so applying it would blank the row on the
// detail page of every channel whose last check failed - the one page where you most need
// to see it. isChannelHealthHidden() carries the same guard; both are needed, because this
// is a CSS rule and that is the JS predicate, and neither goes through the other.
function applyShowFailed() {
  const body = document.getElementById('guide-wrap');
  if (body) body.classList.toggle('hide-failed-channels', !SINGLE_CHANNEL && !LAYOUT.show_failed);
}

function wireLayoutPopover() {
  const wrap = document.getElementById('guide-layout-wrap');
  const pop = document.getElementById('guide-layout-pop');
  const btn = document.getElementById('btn-layout');
  if (!wrap || !pop || !btn) return;

  btn.addEventListener('click', e => {
    e.stopPropagation();
    // The same ⚙ control opens the desktop popover or mobile's Layout sheet (13.6) - one
    // button, one stored fact, two surfaces.
    if (isMobileGuide()) { pop.hidden = true; openLayoutSheet(); return; }
    closePopovers(pop);
    pop.hidden = !pop.hidden;
    syncScrollLock();
  });
  // util.js's backstop listener is registered while base.html's <head> loads, so it runs
  // BEFORE this one and would read a DOM that has not been updated yet - the lock would only
  // release on the following click. Every guide close path syncs for itself.
  document.addEventListener('click', e => {
    if (!wrap.contains(e.target)) { pop.hidden = true; syncScrollLock(); }
  });

  pop.querySelectorAll('input[data-layout]').forEach(cb => {
    cb.addEventListener('change', () => {
      LAYOUT[cb.dataset.layout] = cb.checked;
      saveLayout();
      if (cb.dataset.layout === 'show_failed') applyShowFailed();
      if (cb.dataset.layout === 'collapse_gaps') {
        const area = document.getElementById('guide-program-area');
        if (LAYOUT.collapse_gaps) {
          if (area) area.scrollLeft = 0;
        } else {
          scrollToNow();
        }
      }
      renderGuide();
    });
  });
}

function closePopovers(except) {
  ['guide-layout-pop', 'guide-sort-pop', 'guide-daypicker'].forEach(id => {
    const el = document.getElementById(id);
    if (el && el !== except) el.hidden = true;
  });
  syncScrollLock();
}

// ── Sort control (DESIGN.md 12.4 as amended; shared with mobile per 13.7) ─────

function sortSpec() {
  return SORT_KEYS.find(s => s.key === sortKey) || SORT_KEYS[0];
}

// Lifetime score with the manual adjustment folded in, clamped to 0-100; null when the
// channel has never produced one. This is the single source for both the badge's number and
// its colour, which is what makes it impossible for the two to disagree (DESIGN.md 12.4).
function effectiveHealthScore(ch) {
  if (ch.health_score === null || ch.health_score === undefined) return null;
  const raw = Math.round(ch.health_score + (ch.manual_health_adjustment || 0));
  return Math.max(0, Math.min(100, raw));
}

// The band key ('great' | 'good' | 'fair' | 'poor' | 'untested') from util.js's shared
// reader, which bands from the same declared list the server does (app/health_bands.py).
function healthState(ch) {
  return healthBand(effectiveHealthScore(ch));
}

// Health's "natural" order is good-first, i.e. descending score - so the raw comparator is
// negated for that key and `sortReversed` then flips whatever the natural order was.
//
// A never-tested channel ranks BELOW the worst score rather than sorting last in both
// directions: bottom when good-first, TOP when bad-first (12.4 as amended 2026-07-20,
// superseding the original nulls-last rule and mockup 07's implementation of it). Mapping a
// null score to -Infinity is exactly that statement - it sits below every real score on the
// value axis, and the direction does the rest, so the two directions can never disagree
// about where untested channels go.
function sortValue(ch) {
  if (sortKey === 'name') return (ch.name || '').toLowerCase();
  if (sortKey === 'health') {
    const score = effectiveHealthScore(ch);
    return score === null ? -Infinity : score;
  }
  return null;  // guide order - the server's order, no value needed
}

function sortedChannelIds() {
  const withOrder = channelData.map((ch, i) => ({ ch, i }));
  if (sortKey === 'guide') {
    return (sortReversed ? withOrder.slice().reverse() : withOrder).map(o => String(o.ch.id));
  }
  const dirSign = (sortKey === 'health' ? -1 : 1) * (sortReversed ? -1 : 1);
  return withOrder.slice().sort((A, B) => {
    const a = sortValue(A.ch), b = sortValue(B.ch);
    if (a < b) return -1 * dirSign;
    if (a > b) return 1 * dirSign;
    return A.i - B.i;   // stable: equal keys keep guide order
  }).map(o => String(o.ch.id));
}

// Reorders the existing row nodes rather than rebuilding them - the channel column is
// server-rendered and the program rows hold the renderer's own child nodes.
function applySort() {
  const col = document.getElementById('guide-channel-col');
  const rows = document.getElementById('guide-program-rows');
  if (!col || !rows) return;
  const order = sortedChannelIds();
  order.forEach(id => {
    const crow = col.querySelector(`.guide-channel-row[data-channel-id="${id}"]`);
    const prow = rows.querySelector(`.guide-row[data-channel-id="${id}"]`);
    if (crow) col.appendChild(crow);
    if (prow) rows.appendChild(prow);
  });
  updateSortLabel();
}

// The single sort mutator, shared by desktop's corner-cell menu and mobile's Layout sheet
// (13.7: the behaviour is defined once and identical on both, only the entry point differs).
// Picking the active key flips direction; picking a different one resets direction to that
// key's natural default.
function setSort(key) {
  if (key === sortKey) {
    sortReversed = !sortReversed;
  } else {
    sortKey = key;
    sortReversed = false;
  }
  saveSortState();
  applySort();
}

function updateSortLabel() {
  const spec = sortSpec();
  const fieldEl = document.getElementById('guide-sort-field');
  const arrowEl = document.getElementById('guide-sort-arrow');
  if (fieldEl) fieldEl.textContent = sortKey === 'guide' && !sortReversed ? '' : spec.label;
  if (arrowEl) {
    arrowEl.textContent = sortKey === 'guide' && !sortReversed ? '' : (sortReversed ? '▴' : '▾');
  }
}

function renderSortOptions() {
  const box = document.getElementById('guide-sort-opts');
  if (!box) return;
  const spec = sortSpec();
  box.innerHTML = SORT_KEYS.map(s => {
    const active = s.key === sortKey;
    const dirLabel = active ? (sortReversed ? s.reversed : s.natural) : '';
    return `<button type="button" class="guide-pop-row${active ? ' is-active' : ''}" data-sort-key="${s.key}">` +
      `${escHtml(s.label)}` +
      (active ? `<span class="guide-pop-row-arrow">${escHtml(dirLabel)} ${sortReversed ? '▴' : '▾'}</span>` : '') +
      '</button>';
  }).join('');
}

function wireSortControl() {
  const btn = document.getElementById('guide-sort-btn');
  const pop = document.getElementById('guide-sort-pop');
  if (!btn || !pop) return;

  btn.addEventListener('click', e => {
    e.stopPropagation();
    closePopovers(pop);
    renderSortOptions();
    pop.hidden = !pop.hidden;
    syncScrollLock();
  });
  document.addEventListener('click', e => {
    if (!btn.parentElement.contains(e.target)) { pop.hidden = true; syncScrollLock(); }
  });

  pop.addEventListener('click', e => {
    const row = e.target.closest('[data-sort-key]');
    if (!row) return;
    const key = row.dataset.sortKey;
    setSort(key);
    renderSortOptions();
  });

  updateSortLabel();
}

// ── Row height (DESIGN.md 12.5) ──────────────────────────────────────────────

// Rows size to the tallest cell EITHER side produces - program cells and the channel column -
// so no enabled combination of fields can clip. Recomputed on every render because the field
// set, the filter and the data all move it.
const CELL_FIELD_HEIGHT = {
  rec_status: CELL_LINE.rec, title: CELL_LINE.title, subtitle: CELL_LINE.subtitle,
  description: CELL_LINE.description, time: CELL_LINE.meta, tag_dots: CELL_LINE.meta,
};

// The order fields render in: 12.5's fixed sequence on desktop, the user's own on mobile
// (13.6/13.9). A stored order is repaired rather than trusted - an unknown name is dropped
// and a missing one appended - so a pref written by an older build can neither hide a field
// nor name one that no longer exists.
function cellFieldOrder() {
  if (!isMobileGuide()) return CELL_ORDER_DESKTOP;
  const stored = Array.isArray(LAYOUT.field_order) ? LAYOUT.field_order : [];
  const order = stored.filter((f, i) => CELL_FIELDS.includes(f) && stored.indexOf(f) === i);
  CELL_FIELDS.forEach(f => { if (!order.includes(f)) order.push(f); });
  return order;
}

// The ordered list of fields a cell will actually render. Both the markup builder and the
// row-height calculation go through this, so the height can never be computed for a
// different field set than the one drawn - which is the whole basis of 12.5's no-clip rule.
function cellActiveFields(prog) {
  const real = !prog.is_dummy && prog.title;
  const on = new Set(['title']);
  if (LAYOUT.rec_status && prog.has_recording) on.add('rec_status');
  if (LAYOUT.subtitle && real && prog.sub_title) on.add('subtitle');
  // 13.9 is a hard rule, not a default: a description never renders in a phone-width cell,
  // it lives in the program sheet. There is no checkbox to turn it on there.
  if (!isMobileGuide() && LAYOUT.description && real && prog.description) on.add('description');
  if (LAYOUT.start_time || LAYOUT.end_time || LAYOUT.duration) on.add('time');
  // Tag dots always take their own line when shown (12.5).
  if (LAYOUT.tag_dots && (prog.matched_tags || []).length) on.add('tag_dots');
  return cellFieldOrder().filter(f => on.has(f));
}

function cellContentHeight(prog) {
  const fields = cellActiveFields(prog);
  return fields.reduce((n, f) => n + CELL_FIELD_HEIGHT[f], 0) +
    Math.max(0, fields.length - 1) * CELL_LINE.gap;
}

function techFieldsEnabled() {
  // The tech readout is desktop-only (13.5) - at mobile the numbers live in the channel
  // sheet, so the second row is not rendered and must not be counted into the row height.
  return !isMobileGuide() &&
    (LAYOUT.ch_resolution || LAYOUT.ch_fps || LAYOUT.ch_bitrate || LAYOUT.ch_audio);
}

function channelContentHeight() {
  // A single-channel embed draws no channel column, so it must not be held open by one -
  // the ch_* fields are still at their defaults there because the card offers no control
  // over them, and honouring those would size every row for a column nobody can see.
  if (SINGLE_CHANNEL) return 0;
  if (isMobileGuide()) {
    return LAYOUT.ch_logo_only
      ? CHAN_LINE_MOBILE.logo + CHAN_LINE_MOBILE.padV
      : CHAN_LINE_MOBILE.logo + CHAN_LINE_MOBILE.gap + CHAN_LINE_MOBILE.name + CHAN_LINE_MOBILE.padV;
  }
  return CHAN_LINE.top + (techFieldsEnabled() ? CHAN_LINE.gap + CHAN_LINE.tech : 0) + CHAN_LINE.padV;
}

function computeRowHeight(terms) {
  let max = 0;
  const collapsing = isCollapseActive();
  channelData.forEach(ch => {
    if (isChannelHealthHidden(ch)) return;
    (ch.programs || []).forEach(prog => {
      if (collapsing && !programMatches(prog, terms)) return;
      const h = cellContentHeight(prog);
      if (h > max) max = h;
    });
  });
  return Math.max(44, max + CELL_LINE.padV + CELL_LINE.offset, channelContentHeight());
}

// ── Header track sync (DESIGN.md 12.6) ───────────────────────────────────────

// The header track sits outside the horizontal scroller so the whole grid header can stick to
// the viewport; that means its horizontal position has to be driven from the scroller.
function syncHeadTrack() {
  const area = document.getElementById('guide-program-area');
  const track = document.getElementById('guide-head-track');
  if (!area || !track) return;
  const x = area.scrollLeft;
  track.style.transform = `translateX(${-x}px)`;

  // Keep each day's label visible while any part of its day is still on screen, instead of
  // it scrolling away with the day's start. The nested `position: sticky` that did this
  // cannot work here (a transformed track never scrolls, so sticky has nothing to resolve
  // against), so the same offset is applied directly.
  const vpWidth = area.clientWidth;
  track.querySelectorAll('.guide-day-segment').forEach(seg => {
    const label = seg.querySelector('.guide-day-segment-label');
    if (!label) return;
    const segLeft = seg.offsetLeft;
    const room = seg.offsetWidth - label.offsetWidth;
    const offset = Math.max(0, Math.min(x - segLeft, room));
    label.style.transform = offset > 0 ? `translateX(${offset}px)` : '';
    seg.classList.toggle('is-offscreen', segLeft + seg.offsetWidth < x || segLeft > x + vpWidth);
  });
}

// The grid header sticks below the toolbar, so its offset is the toolbar's REAL rendered
// height read from the DOM. The toolbar's height legitimately varies (it wraps at narrow
// widths), and a hardcoded constant is exactly how the row-height / sticky-offset pairs in
// this codebase have desynced before.
function measureToolbarHeight() {
  const bar = document.getElementById('guide-toolbar');
  if (!bar) return;
  const h = Math.round(bar.getBoundingClientRect().height);
  if (h > 0) document.documentElement.style.setProperty('--guide-tbh', `${h}px`);
}

// The mobile shell's own sticky top bar is the FIRST thing in the sticky stack, so the
// guide toolbar has to start below it, not at 0. Measured, not hardcoded, and unconditional:
// .topnav appears at ≤900px while the guide's own mobile block starts at ≤768px, so a
// value tied to either breakpoint is wrong across the 132px in between. A hidden top bar
// measures 0 and the whole stack collapses back to the desktop arrangement on its own.
function measureTopBarHeight() {
  const bar = document.querySelector('.topnav');
  const h = (bar && getComputedStyle(bar).display !== 'none')
    ? Math.round(bar.getBoundingClientRect().height) : 0;
  document.documentElement.style.setProperty('--guide-topbarh', `${h}px`);
}

// Everything the breakpoint decides, re-decided. Crossing it changes the time scale, which
// stored Layout applies, and the whole sticky stack - so the grid is rebuilt rather than
// left half in one mode.
function applyBreakpoint() {
  PIXELS_PER_MINUTE = isMobileGuide() ? PX_PER_MIN_MOBILE : PX_PER_MIN_DESKTOP;
  reloadLayoutForBreakpoint();
  applyChannelColumnStyle();
  applyShowFailed();
  measureTopBarHeight();
  measureToolbarHeight();
}

// 13.5's logo-only channel column is a class on the grid wrapper, so the width lives in one
// CSS rule rather than being written onto every row.
function applyChannelColumnStyle() {
  const outer = document.querySelector('.guide-outer');
  if (outer) outer.classList.toggle('guide-logo-only', isMobileGuide() && !!LAYOUT.ch_logo_only);
}

// ── Fetch & render ───────────────────────────────────────────────────────────

function fetchAndRender() {
  // No-op on pages that embed only the search/record modals, not the guide grid
  // (recording detail) - their GUIDE_CONFIG has no epgUrl and initWindow() never ran.
  if (!GUIDE_CONFIG.epgUrl || !windowStart) return;
  const area = document.getElementById('guide-program-area');
  const savedScroll = area ? area.scrollLeft : 0;
  const isInitial = savedScroll === 0 && !channelData.length;

  const startIso = windowStart.toISOString().replace('Z', '');
  const endIso   = windowEnd.toISOString().replace('Z', '');
  let url = `${GUIDE_CONFIG.epgUrl}?start=${encodeURIComponent(startIso)}&end=${encodeURIComponent(endIso)}`;
  if (GUIDE_CONFIG.channelId) url += `&channel_id=${GUIDE_CONFIG.channelId}`;

  fetch(url)
    .then(r => {
      if (!r.ok) throw new Error(`EPG API ${r.status}`);
      return r.json();
    })
    .then(data => {
      channelData = data.channels || [];
      renderGuide();
      if (isInitial) {
        scrollToNow();
      } else {
        if (area) area.scrollLeft = savedScroll;
      }
    })
    .catch(err => {
      console.error('EPG fetch failed:', err);
    });
}

// ── Day header ───────────────────────────────────────────────────────────────

function getDayBoundariesInRange(rangeStart, rangeEnd) {
  if (!rangeStart || !rangeEnd) return [];
  const boundaries = [];

  // Calendar arithmetic, not display: the y/m/d of the range's first day in the display
  // timezone, walked forward below. Through util.js's cached formatter because the mobile
  // day bar calls this once per collapsed segment.
  const p = {};
  tzFormatter({ year: 'numeric', month: '2-digit', day: '2-digit' })
    .formatToParts(rangeStart).forEach(({ type, value }) => { p[type] = value; });
  let y = +p.year, m = +p.month, d = +p.day;

  let segStart = rangeStart;
  while (segStart < rangeEnd) {
    const nd = new Date(Date.UTC(y, m - 1, d, 12, 0, 0));
    nd.setUTCDate(nd.getUTCDate() + 1);
    const dayEnd = tzInputValueToDate(
      `${nd.getUTCFullYear()}-${String(nd.getUTCMonth() + 1).padStart(2, '0')}-${String(nd.getUTCDate()).padStart(2, '0')}T00:00`,
      TZ);
    const segEnd = dayEnd < rangeEnd ? dayEnd : rangeEnd;

    boundaries.push({ start: segStart, end: segEnd });

    segStart = segEnd;
    y = nd.getUTCFullYear(); m = nd.getUTCMonth() + 1; d = nd.getUTCDate();
  }
  return boundaries;
}

function getDayBoundariesInWindow() {
  return getDayBoundariesInRange(windowStart, windowEnd);
}

function renderDayHeader() {
  const bar = document.getElementById('guide-day-header');
  if (!bar) return;
  bar.innerHTML = '';
  const totalMin = (windowEnd - windowStart) / 60000;
  bar.style.width = (totalMin * PIXELS_PER_MINUTE) + 'px';

  getDayBoundariesInWindow().forEach(({ start, end }) => {
    const widthPx = ((end - start) / 60000) * PIXELS_PER_MINUTE;
    const seg = document.createElement('div');
    seg.style.width = widthPx + 'px';

    const full = fmtDateTz(start);
    const isNarrow = widthPx < 150;
    seg.className = 'guide-day-segment' + (isNarrow ? ' narrow' : '');
    seg.title = full;

    // Label is a nested sticky element so it stays visible (pinned to the left edge of the
    // scroll viewport) for as long as any part of its day is still in view, instead of
    // scrolling away with the segment's start.
    const label = document.createElement('span');
    label.className = 'guide-day-segment-label';
    label.textContent = isNarrow
      ? fmtDateTz(start, { weekday: 'short', month: 'short', day: 'numeric' })
      : full;
    seg.appendChild(label);

    bar.appendChild(seg);
  });
}

// ── Time header ──────────────────────────────────────────────────────────────

function renderTimeHeader() {
  const header = document.getElementById('guide-time-header');
  header.innerHTML = '';
  const totalMin = (windowEnd - windowStart) / 60000;
  header.style.width = (totalMin * PIXELS_PER_MINUTE) + 'px';

  for (let m = 0; m < totalMin; m += SLOT_MINUTES) {
    const slotDate = new Date(windowStart.getTime() + m * 60000);
    const slot = document.createElement('div');
    slot.style.width = (SLOT_MINUTES * PIXELS_PER_MINUTE) + 'px';
    slot.className = 'guide-time-slot';
    slot.textContent = fmtTimeTz(slotDate);
    header.appendChild(slot);
  }
}

// ── Recording state helpers ───────────────────────────────────────────────────

function recCssClass(status) {
  switch (status) {
    case 'IN_PROGRESS':  return 'rec-active';
    case 'PAUSED':       return 'rec-paused';
    case 'RETRYING':     return 'rec-retry';
    case 'SCHEDULED':    return 'rec-scheduled';
    case 'CONCATENATING':
    case 'ANALYZING':
    case 'CONVERTING':
    case 'COMPLETED':    return 'rec-done';
    default:             return 'rec-done';
  }
}

// The chip's meaning is carried by its colour, so it carries a tooltip with a distinct text
// per state rather than relying on the colour alone (DESIGN.md 12.7). Every state is named
// explicitly - a trailing `default` that renders a real state is how the next status added
// lands somewhere wrong without erroring.
function recStatusBadge(status) {
  const chip = (cls, text, tip) =>
    `<div class="rec-badge rec-badge-${cls} tip-plain" data-tip="${escTipAttr(tip)}">${text}</div>`;
  switch (status) {
    case 'SCHEDULED':
      return chip('scheduled', 'Scheduled',
        'Scheduled\nA recording is set up for this program but has not started yet.');
    case 'IN_PROGRESS':
      return chip('active', '⏺ Recording',
        'Recording now\nCapture is running for this program.');
    case 'PAUSED':
      return chip('paused', '⏸ Paused',
        'Paused\nThe recording for this program is paused and is not capturing.');
    case 'RETRYING':
      return chip('retry', '↻ Retrying',
        'Retrying\nThe stream reconnected but immediately died - waiting to try again.');
    case 'CONCATENATING':
      return chip('done', '✓ Recorded',
        'Recorded\nCapture finished; the segments are being joined into the final file.');
    case 'ANALYZING':
      return chip('done', '✓ Recorded',
        'Recorded\nCapture finished and joined; the file is being checked before conversion.');
    case 'CONVERTING':
      return chip('done', '✓ Converting',
        'Converting\nCapture finished; the file is being converted to its final format.');
    case 'COMPLETED':
      return chip('done', '✓ Recorded',
        'Recorded\nThis program was recorded and the file is in your library.');
    default:
      return '';
  }
}

// Tooltips travel through an HTML attribute, so newlines are encoded and the shared portal
// tooltip in util.js restores them.
function escTipAttr(text) {
  return escHtml(text).replace(/\n/g, '&#10;');
}

// ── Tags: highlight badges + include/exclude filter ───────────────────────────

// 12.5: tag dots always take their own line when shown. 12.7: a dot's meaning is carried
// only by colour, so each one gets a tooltip naming the tag and what it implies - and each
// sits in a padded hit wrapper, because a 7px dot is not reliably hoverable.
function tagBadgesHtml(matchedTags) {
  if (!matchedTags || !matchedTags.length) return '';
  const dots = matchedTags.map(t => {
    const tip = `Tag: ${t.name}\nThis program matched your "${t.name}" tag rule, which is why the dot is here.`;
    return `<span class="tag-badge-hit tip-plain" data-tip="${escTipAttr(tip)}">` +
      `<span class="tag-badge-dot" style="background:${escHtml(t.color)}"></span></span>`;
  }).join('');
  return '<div class="tag-badges">' + dots + '</div>';
}

function tagFilterState() {
  const includeIds = Array.from(document.querySelectorAll('.tag-filter-include:checked')).map(cb => cb.value);
  const excludeIds = Array.from(document.querySelectorAll('.tag-filter-exclude:checked')).map(cb => cb.value);
  const modeEl = document.querySelector('input[name="tag-filter-mode"]:checked');
  return { includeIds, excludeIds, mode: modeEl ? modeEl.value : 'any' };
}

function tagFilterActive() {
  const { includeIds, excludeIds } = tagFilterState();
  return includeIds.length > 0 || excludeIds.length > 0;
}

// Core tag predicate over an array of string tag ids and a {includeIds, excludeIds, mode}
// state. Reached through programPassesTagFilter() from the guide toolbar filter; kept
// separate from it because the predicate is about tag ids and nothing else.
function tagIdsPass(tagIds, state) {
  const { includeIds, excludeIds, mode } = state;
  if (excludeIds.length && excludeIds.some(id => tagIds.includes(id))) return false;
  if (!includeIds.length) return true;
  return mode === 'all'
    ? includeIds.every(id => tagIds.includes(id))
    : includeIds.some(id => tagIds.includes(id));
}

function programPassesTagFilter(prog, state) {
  const tagIds = (prog.matched_tags || []).map(t => String(t.id));
  return tagIdsPass(tagIds, state || tagFilterState());
}

function updateTagFilterSummary() {
  const summary = document.getElementById('tag-filter-summary');
  if (!summary) return;
  const { includeIds, excludeIds } = tagFilterState();
  const n = includeIds.length + excludeIds.length;
  summary.textContent = n ? `Tags (${n})` : 'Tags';
  summary.classList.toggle('has-value', n > 0);
}

// ── Duration filter ──────────────────────────────────────────────────────────
// The grid's own program-length filter. EPG Deep Search (`/channels`) has an equivalent
// bound against app/channel_search.py's `duration` dimension, but that one re-runs a DB
// query per filter change - the grid never re-queries, it filters in memory over the window
// it already fetched (same as the tag filter beside it), so the length is derived from
// start_time/stop_time exactly the way the mobile program sheet already computes one
// program's duration (openProgramSheet). The "Longer than X hours"/"Shorter than X minutes"
// phrasing and the minutes/hours unit toggle are duplicated from channel-search.js's
// duration facet on purpose, not shared code - that page's version is wired into a
// URL-persisted facet system this filter has no need of; state here lives only in the
// panel's own inputs, the same as the tag filter.

function durationFilterState() {
  const minEl = document.getElementById('dur-filter-min');
  const maxEl = document.getElementById('dur-filter-max');
  const unitEl = document.getElementById('dur-filter-unit');
  const unit = unitEl && unitEl.value === 'minutes' ? 'minutes' : 'hours';
  const toMinutes = raw => {
    const n = raw === '' || raw == null ? NaN : Number(raw);
    if (!Number.isFinite(n) || n <= 0) return null;
    return unit === 'hours' ? Math.round(n * 60) : Math.round(n);
  };
  return {
    min: minEl ? toMinutes(minEl.value) : null,
    max: maxEl ? toMinutes(maxEl.value) : null,
    unit,
  };
}

function durationFilterActive(state) {
  const s = state || durationFilterState();
  return s.min !== null || s.max !== null;
}

function programDurationMinutes(prog) {
  return (new Date(prog.stop_time + 'Z') - new Date(prog.start_time + 'Z')) / 60000;
}

function programPassesDurationFilter(prog, state) {
  const s = state || durationFilterState();
  if (s.min === null && s.max === null) return true;
  const mins = programDurationMinutes(prog);
  if (s.min !== null && mins < s.min) return false;
  if (s.max !== null && mins > s.max) return false;
  return true;
}

// "Longer than 3 hours" / "Shorter than 30 minutes" / "3-4 hours" - the same phrasing
// channel-search.js's duration facet already established for the same underlying idea.
function durationFilterLabel(state) {
  const s = state || durationFilterState();
  const disp = mins => (s.unit === 'hours' ? Math.round((mins / 60) * 10) / 10 : mins);
  const word = n => (n === 1 ? s.unit.slice(0, -1) : s.unit);
  if (s.min !== null && s.max !== null) return `${disp(s.min)}-${disp(s.max)} ${s.unit}`;
  if (s.min !== null) return `Longer than ${disp(s.min)} ${word(disp(s.min))}`;
  if (s.max !== null) return `Shorter than ${disp(s.max)} ${word(disp(s.max))}`;
  return 'Length';
}

function updateDurFilterSummary() {
  const summary = document.getElementById('dur-filter-summary');
  if (!summary) return;
  const state = durationFilterState();
  const active = durationFilterActive(state);
  summary.textContent = active ? durationFilterLabel(state) : 'Length';
  summary.classList.toggle('has-value', active);
}

// Resets the real filter inputs (the single source of state - see durationFilterState()) and
// re-renders. Shared by the desktop panel's own Clear button and the mobile sheet's, so the
// two can't drift: the sheet syncs its own visible fields separately after calling this,
// since those are copies, not the source (openLengthSheet's own comment explains why).
function clearDurationFilter() {
  const minEl = document.getElementById('dur-filter-min');
  const maxEl = document.getElementById('dur-filter-max');
  const unitEl = document.getElementById('dur-filter-unit');
  if (minEl) minEl.value = '';
  if (maxEl) maxEl.value = '';
  if (unitEl) unitEl.value = 'hours';
  updateDurFilterSummary();
  renderGuide();
}

// ── Program rows ─────────────────────────────────────────────────────────────

// Rows are addressed by channel id, never by index: the sort control reorders the row nodes,
// so DOM order and channelData order legitimately differ.
function channelRowFor(id) {
  return document.querySelector(`.guide-channel-row[data-channel-id="${id}"]`);
}

function programRowFor(id) {
  return document.querySelector(`.guide-row[data-channel-id="${id}"]`);
}

// One builder for a program cell's inner markup, shared by the normal and collapsed render
// paths so the enabled field set can never differ between them. Field order is fixed by
// DESIGN.md 12.5: recording chip, title, subtitle, description, time line, tag dots.
function programCellHtml(prog, ch) {
  return cellActiveFields(prog).map(f => cellFieldHtml(f, prog, ch)).join('');
}

function cellFieldHtml(field, prog, ch) {
  switch (field) {
    case 'rec_status': return recStatusBadge(prog.recording_status);
    case 'title':      return `<div class="guide-program-title">${escHtml(prog.title || ch.name)}</div>`;
    case 'subtitle':   return `<div class="guide-program-subtitle">${escHtml(prog.sub_title)}</div>`;
    case 'description':return `<div class="guide-program-desc">${escHtml(prog.description)}</div>`;
    case 'time':       return `<div class="guide-program-time">${escHtml(programTimeLine(prog))}</div>`;
    case 'tag_dots':   return tagBadgesHtml(prog.matched_tags);
    // No trailing default that renders something: a field cellActiveFields() admitted but
    // this switch does not know about is a bug, and a silent '' would hide it while the row
    // height already reserved a line for it (CLAUDE.md: states are enumerated).
  }
  console.error(`guide: no renderer for cell field ${field}`);
  return '';
}

// Start / end / duration are three separate settings that share one line (12.3's Time group).
// Returns '' when all three are off, so the line reserves no space (12.5).
function programTimeLine(prog) {
  const fmt = iso => fmtTimeTz(utcIsoToDate(iso));
  const bits = [];
  if (LAYOUT.start_time && LAYOUT.end_time) {
    bits.push(`${fmt(prog.start_time)} - ${fmt(prog.stop_time)}`);
  } else if (LAYOUT.start_time) {
    bits.push(fmt(prog.start_time));
  } else if (LAYOUT.end_time) {
    bits.push(`until ${fmt(prog.stop_time)}`);
  }
  if (LAYOUT.duration) {
    const mins = Math.round((new Date(prog.stop_time + 'Z') - new Date(prog.start_time + 'Z')) / 60000);
    bits.push(fmtDur(mins * 60, false));   // the static DESIGN.md 9.1 form, not a live ticker
  }
  return bits.join('  ·  ');
}

function renderProgramRows() {
  const totalMin = (windowEnd - windowStart) / 60000;

  channelData.forEach(ch => {
    const row = programRowFor(ch.id);
    if (!row) return;
    row.innerHTML = '';
    row.style.width = (totalMin * PIXELS_PER_MINUTE) + 'px';

    // Recording band + outline: band fills gaps behind cells; outline provides a unified border on top
    // A channel can have multiple recordings scheduled in the visible window, so draw one band per recording.
    (ch.recordings || []).forEach(rec => {
      const recStart = new Date(rec.start_time + 'Z');
      const recStop  = new Date(rec.stop_time  + 'Z');
      const bandLeftMin  = Math.max(0, (recStart - windowStart) / 60000);
      const bandRightMin = Math.min(totalMin, (recStop - windowStart) / 60000);
      if (bandRightMin > bandLeftMin) {
        const leftPx  = bandLeftMin * PIXELS_PER_MINUTE;
        const widthPx = (bandRightMin - bandLeftMin) * PIXELS_PER_MINUTE;

        const bandStatusClass = ' ' + recCssClass(rec.status);
        const band = document.createElement('div');
        band.className = 'guide-recording-band' + bandStatusClass;
        band.style.left  = leftPx + 'px';
        band.style.width = widthPx + 'px';
        row.appendChild(band);

        const outline = document.createElement('div');
        outline.className = 'guide-recording-outline' + bandStatusClass;
        outline.style.left  = leftPx + 'px';
        outline.style.width = widthPx + 'px';
        row.appendChild(outline);
      }
    });

    (ch.programs || []).forEach((prog, progIdx) => {
      const pStart = new Date(prog.start_time + 'Z');
      const pStop  = new Date(prog.stop_time  + 'Z');

      // Clamp to window edges for positioning
      const visStartMin = Math.max(0, (pStart - windowStart) / 60000);
      const visStopMin  = Math.min(totalMin, (pStop - windowStart) / 60000);
      if (visStopMin <= visStartMin) return;

      const leftPx  = visStartMin * PIXELS_PER_MINUTE;
      const widthPx = Math.max(2, (visStopMin - visStartMin) * PIXELS_PER_MINUTE - 2);

      const el = document.createElement('div');
      const progRecClass = prog.has_recording ? (' has-recording ' + recCssClass(prog.recording_status)) : '';
      el.className = 'guide-program' + progRecClass + (prog.is_dummy ? ' is-dummy' : '');
      if (widthPx < 60) el.classList.add('narrow');
      el.style.left  = leftPx + 'px';
      el.style.width = widthPx + 'px';
      el.dataset.progIdx = progIdx;

      // Precise partial highlight: clip gradient to actual recording bounds
      if (prog.has_recording && prog.recording_start_time) {
        const recStart = new Date(prog.recording_start_time + 'Z');
        const recStop  = new Date(prog.recording_stop_time  + 'Z');
        const overlapStart = Math.max(pStart.getTime(), recStart.getTime());
        const overlapEnd   = Math.min(pStop.getTime(),  recStop.getTime());
        // Gradient percentages must be relative to the element's visible span (clamped to
        // window edges), not the full program duration - otherwise left-clipped programs
        // (start before window) produce a fill that begins too far to the right.
        const elemStart    = Math.max(pStart.getTime(), windowStart.getTime());
        const elemEnd      = Math.min(pStop.getTime(),  windowEnd.getTime());
        const elemDuration = elemEnd - elemStart;
        const leftPct  = ((overlapStart - elemStart) / elemDuration * 100).toFixed(2);
        const rightPct = ((overlapEnd   - elemStart) / elemDuration * 100).toFixed(2);
        el.style.setProperty('--rec-left',  leftPct  + '%');
        el.style.setProperty('--rec-right', rightPct + '%');
      }

      el.innerHTML = programCellHtml(prog, ch);

      el.addEventListener('click', () => openProgramTarget(prog, ch));
      row.appendChild(el);
    });
  });
}

// ── Midnight lines ───────────────────────────────────────────────────────────

function renderMidnightLines() {
  document.querySelectorAll('.guide-midnight-line').forEach(el => el.remove());
  // Appended to guide-program-area (not guide-program-rows) and spans its full height via
  // top:0/bottom:0, so the line runs through the day bar and time header too, not just the rows.
  const area = document.getElementById('guide-program-area');
  if (!area || !windowStart) return;

  const boundaries = getDayBoundariesInWindow();
  // Every boundary's `end` (except the window's own clipped end, i.e. the last segment) is an
  // actual midnight crossing - the window itself doesn't start or end on one.
  for (let i = 0; i < boundaries.length - 1; i++) {
    const midnight = boundaries[i].end;
    const offsetMin = (midnight - windowStart) / 60000;
    const line = document.createElement('div');
    line.className = 'guide-midnight-line';
    line.style.left = (offsetMin * PIXELS_PER_MINUTE) + 'px';
    area.appendChild(line);
  }
}

// ── Now line ─────────────────────────────────────────────────────────────────

function updateNowLine() {
  document.querySelectorAll('.guide-now-line').forEach(el => el.remove());

  const now = new Date();
  if (now <= windowStart || now >= windowEnd) return;

  const offsetMin = (now - windowStart) / 60000;
  const leftPx = offsetMin * PIXELS_PER_MINUTE;

  const line = document.createElement('div');
  line.className = 'guide-now-line';
  line.style.left = leftPx + 'px';

  const rows = document.getElementById('guide-program-rows');
  if (rows) rows.appendChild(line);
}

// The scroll-position readouts live in the .timenav segmented control now (12.2);
// only its debounce timer survives from the standalone label pair that preceded it.
let labelDebounce = null;

// ── Segmented-control readout + day picker (DESIGN.md 12.2 as amended) ────────

// Live output: the day and time range currently on screen, read from scroll position. It is
// never replaced by an input - clicking it opens the day picker below instead.
function updateReadout() {
  const dayEl = document.getElementById('guide-readout-day');
  const timeEl = document.getElementById('guide-readout-time');
  if (!dayEl || !timeEl) return;

  if (isCollapseActive() && collapsedSegments.length) {
    const n = collapsedSegments.length;
    dayEl.textContent = `${n} matching block${n === 1 ? '' : 's'}`;
    timeEl.textContent = n === 1
      ? fmtDateTz(collapsedSegments[0].start, { weekday: 'short', month: 'short', day: 'numeric' })
      : 'multiple dates';
    return;
  }

  const area = document.getElementById('guide-program-area');
  const scrollLeft = area ? area.scrollLeft : 0;
  const viewWidth = area ? area.clientWidth : 0;
  const visStart = new Date(windowStart.getTime() + (scrollLeft / PIXELS_PER_MINUTE) * 60000);
  const visEnd = new Date(visStart.getTime() + (viewWidth / PIXELS_PER_MINUTE) * 60000);

  const fmtTime = dt => fmtTimeTz(dt);
  const fmtDay = dt => fmtDateTz(dt);

  const startDay = fmtDay(visStart);
  const endDay = fmtDay(visEnd);
  dayEl.textContent = startDay === endDay ? startDay : `${startDay} - ${endDay}`;
  timeEl.textContent = `${fmtTime(visStart)} - ${fmtTime(visEnd)}`;
}

// The day list is generated from the rendered window, so it is capped to it and carries no
// typed day count (12.1). Today jumps to now; every other day jumps to its start.
function renderDayPicker() {
  const pop = document.getElementById('guide-daypicker');
  if (!pop) return;
  const todayKey = dayKey(new Date());
  const rows = getDayBoundariesInWindow().map(({ start }) => {
    const isToday = dayKey(start) === todayKey;
    const label = fmtDateTz(start);
    return `<button type="button" class="guide-pop-row${isToday ? ' is-active' : ''}" ` +
      `data-jump="${start.toISOString()}"${isToday ? ' data-jump-now="1"' : ''}>` +
      `${escHtml(label)}${isToday ? '<span class="guide-pop-row-arrow">Today</span>' : ''}</button>`;
  }).join('');
  pop.innerHTML = '<h4>Jump to day</h4>' + rows;
}

function dayKey(date) {
  return tzDayKey(date);
}

function jumpToInstant(date) {
  const area = document.getElementById('guide-program-area');
  if (!area || !windowStart) return;
  const px = ((date - windowStart) / 60000) * PIXELS_PER_MINUTE;
  area.scrollLeft = Math.max(0, px);
}

// The channel column's ONE click handler. Desktop navigates to the channel's (or group's)
// detail page as it always has; at phone widths 13.1 makes the cell a tap target opening
// the channel sheet instead, because the mini-column has no room for the health score, the
// plain-language why, the tech readout or the owning account.
//
// Both behaviours live in this single listener on purpose. They started as two listeners on
// the same element and both fired on the same click - the sheet opened and the navigation
// immediately blew it away. That is the "one region, one updater" rule in CLAUDE.md, and a
// second listener gated to the other breakpoint would leave the trap set for the next
// person. Delegated on the column, because applySort() reorders the rows and
// renderChannelColumn() rewrites their contents on every render.
function wireChannelTaps() {
  const col = document.getElementById('guide-channel-col');
  if (!col) return;
  col.addEventListener('click', e => {
    const row = e.target.closest('.guide-channel-row');
    if (!row) return;

    if (isMobileGuide()) {
      const ch = channelData.find(c => String(c.id) === row.dataset.channelId);
      if (ch) openChannelSheet(ch);
      return;
    }
    // Group rows carry a synthetic 'g<id>' channel-id; they navigate to the group detail
    // page, not /channels/<id> (which would 404 on the 'g<id>' string).
    if (row.dataset.groupId) {
      window.location.href = GUIDE_CONFIG.groupDetailUrlBase + row.dataset.groupId;
      return;
    }
    if (row.dataset.channelId) {
      window.location.href = GUIDE_CONFIG.chDetailUrlBase + row.dataset.channelId;
    }
  });
}

function wireDayPicker() {
  const readout = document.getElementById('guide-readout');
  const pop = document.getElementById('guide-daypicker');
  if (!readout || !pop) return;

  readout.addEventListener('click', e => {
    e.stopPropagation();
    // Same behaviour, two presentations: a popover anchored under the readout on desktop,
    // a bottom sheet at phone widths (13.3).
    if (isMobileGuide()) { openDaySheet(); return; }
    closePopovers(pop);
    renderDayPicker();
    pop.hidden = !pop.hidden;
    syncScrollLock();
  });
  document.addEventListener('click', e => {
    if (e.target !== readout && !readout.contains(e.target) && !pop.contains(e.target)) {
      pop.hidden = true;
      syncScrollLock();
    }
  });
  pop.addEventListener('click', e => {
    const row = e.target.closest('[data-jump]');
    if (!row) return;
    if (row.dataset.jumpNow) scrollToNow();
    else jumpToInstant(new Date(row.dataset.jump));
    pop.hidden = true;
    syncScrollLock();
    updateReadout();
  });
}

// What a tap on a program cell opens. Desktop goes straight to the scheduling modal, as it
// always has; mobile opens the program sheet, from which "Set Up Recording…" reaches the
// same modal (13.10). One entry point so both render paths - normal and collapsed - can
// never diverge on it.
function openProgramTarget(prog, ch) {
  if (isMobileGuide()) openProgramSheet(prog, ch);
  else openModal(prog, ch);
}

// ── Mobile bottom sheets (DESIGN.md 13.10) ───────────────────────────────────
//
// Touch has no hover, so every 12.7 tooltip surface on the grid becomes a tap target
// opening a sheet (13.1). All five sheets - program, channel, day picker, Layout, Tags -
// go through util.js's buildModal(), which style.css already renders as a bottom sheet at
// ≤768px (9.6). That is deliberately NOT a second overlay component: a guide-local sheet
// would duplicate the backdrop, Esc handling and scroll behaviour the modal already has.

let openSheetEl = null;

function openSheet(opts) {
  closeSheet();
  openSheetEl = buildModal(Object.assign({
    panelClass: 'guide-sheet',
    onClose: () => { openSheetEl = null; },
  }, opts));
  return openSheetEl;
}

function closeSheet() {
  if (openSheetEl && openSheetEl.closeModal) openSheetEl.closeModal();
  openSheetEl = null;
}

function sheetLine(label, value) {
  if (!value) return '';
  return `<div class="guide-sheet-line"><span class="guide-sheet-lbl">${escHtml(label)}</span>` +
    `<span class="guide-sheet-val">${escHtml(value)}</span></div>`;
}

// 13.10's program sheet: the cell's own fields, plus the description that mobile cells are
// forbidden from showing (13.9), plus the state-dependent actions.
function openProgramSheet(prog, ch) {
  const real = !prog.is_dummy && prog.title;
  const fmtT = iso => fmtTimeTz(utcIsoToDate(iso));
  const start = new Date(prog.start_time + 'Z');
  const mins = Math.round((new Date(prog.stop_time + 'Z') - start) / 60000);

  const tags = (prog.matched_tags || []).map(t =>
    `<span class="guide-sheet-tag"><span class="tag-badge-dot" style="background:${escHtml(t.color)}"></span>` +
    `${escHtml(t.name)}</span>`).join('');

  const body =
    (prog.sub_title && real ? `<p class="guide-sheet-sub">${escHtml(prog.sub_title)}</p>` : '') +
    sheetLine('When', `${fmtT(prog.start_time)} - ${fmtT(prog.stop_time)}  ·  ${fmtDur(mins * 60, false)}`) +
    sheetLine('Day', fmtDateTz(start)) +
    sheetLine('Channel', ch.name) +
    (real && prog.description ? `<p class="guide-sheet-desc">${escHtml(prog.description)}</p>` : '') +
    (tags ? `<div class="guide-sheet-tags">${tags}</div>` : '');

  openSheet({ title: prog.title || ch.name, body, footer: programSheetActions(prog, ch) });
}

// The recording states, each named explicitly - no trailing else rendering a real
// state (CLAUDE.md: states are enumerated). "Set Up Recording…" is 13.10's mandated wording:
// it opens the pre-filled scheduling flow and nothing is scheduled until the user confirms
// there, so a bare "Record" implying instant scheduling is banned. RETRYING joins
// IN_PROGRESS/PAUSED here (no active ffmpeg, same as PAUSED, but still an in-flight
// recording - not terminal).
function programSheetActions(prog, ch) {
  const status = prog.recording_status;
  const hasRec = prog.has_recording && prog.recording_id;

  if (hasRec && (status === 'IN_PROGRESS' || status === 'PAUSED' || status === 'RETRYING')) {
    return [
      { label: 'Dashboard →', class: 'btn btn-primary',
        onClick: () => { window.location.href = '/'; return false; } },
      { label: '■ Stop', class: 'btn btn-danger',
        onClick: (close) => { close(); openActiveRecModal(prog); return false; } },
    ];
  }
  if (hasRec && TERMINAL_STATUSES.has(status)) {
    return [{ label: 'View recording', class: 'btn btn-primary',
      onClick: () => { window.location.href = GUIDE_CONFIG.recDetailUrlBase + prog.recording_id; return false; } }];
  }
  if (hasRec) {
    return [{ label: 'Cancel recording', class: 'btn btn-danger',
      onClick: (close) => { close(); cancelScheduledRecording(prog.recording_id); return false; } }];
  }
  return [{ label: 'Set Up Recording…', class: 'btn btn-primary',
    onClick: (close) => { close(); openModal(prog, ch); return false; } }];
}

function cancelScheduledRecording(recId) {
  if (!confirm('Cancel scheduled recording? This scheduled recording will be removed.')) return;
  jsonFetch(GUIDE_CONFIG.editRecordingUrlBase + recId + '/cancel-json', { method: 'POST' })
    .then(() => fetchAndRender())
    .catch(e => showToast(e.message || 'Cancel failed.', { type: 'error' }));
}

// 13.10's channel sheet: the health state in plain language with its number, the tech
// readout the mobile column does not have room for, the owning account, and the way out to
// the channel's own page.
function openChannelSheet(ch) {
  const state = healthState(ch);
  const score = effectiveHealthScore(ch);
  const n = ch.health_score_sample_count || 0;

  let why;
  if (state === 'untested') {
    why = 'This channel has no health score yet. Run a health check to measure it.';
  } else if (ch.last_test_status === 'FAILED') {
    why = `The most recent health check FAILED.${ch.last_test_error_detail ? ' ' + ch.last_test_error_detail : ''}`;
  } else if (ch.last_test_error_detail) {
    why = ch.last_test_error_detail;
  } else {
    why = `Lifetime score over ${n} observation${n === 1 ? '' : 's'}.`;
  }

  const detailUrl = ch.is_group
    ? GUIDE_CONFIG.groupDetailUrlBase + ch.group_id
    : GUIDE_CONFIG.chDetailUrlBase + ch.id;

  const body =
    `<div class="guide-sheet-health hb-${state === 'untested' ? 'none' : state}">` +
    `${state === 'untested' ? '--' : score}<span>/100</span></div>` +
    `<p class="guide-sheet-sub">${escHtml(healthBandName(score))} - ${escHtml(why)}</p>` +
    sheetLine('Stream', channelTechText(ch)) +
    sheetLine('Account', ch.account_name || '') +
    (ch.lifecycle === 'missing'
      ? sheetLine('Status', `Missing since ${ch.lifecycle_date} - no longer seen in ${ch.account_name || 'this account'}'s synced feed.${repointHint(ch, true)}`)
      : '') +
    (ch.is_group ? sheetLine('Group', `${ch.member_count} feeds, recording from ${ch.active_channel_name || '-'}`) : '');

  openSheet({
    title: ch.name,
    body,
    footer: [{ label: ch.is_group ? 'Open group' : 'Open channel', class: 'btn btn-primary',
      onClick: () => { window.location.href = detailUrl; return false; } }],
  });
}

// The tech readout as plain text for the sheet. Same wording as the desktop column's
// `no stream data` (12.4) so the two surfaces never describe the same channel differently.
function channelTechText(ch) {
  const hasData = ch.last_test_status === 'COMPLETED' &&
    (ch.last_test_resolution || ch.last_test_fps || ch.last_test_bitrate_kbps || ch.last_test_audio_codec);
  if (!hasData) return 'no stream data';
  const bits = [];
  if (ch.last_test_resolution) {
    const height = ch.last_test_resolution.split('x')[1];
    bits.push(height ? `${height}p` : ch.last_test_resolution);
  }
  if (ch.last_test_fps) bits.push(`${Math.round(ch.last_test_fps)}fps`);
  if (ch.last_test_bitrate_kbps) bits.push(`${(ch.last_test_bitrate_kbps / 1000).toFixed(1)} Mbps`);
  if (ch.last_test_audio_codec) {
    bits.push(ch.last_test_audio_channels
      ? `${ch.last_test_audio_codec} ${ch.last_test_audio_channels}ch`
      : ch.last_test_audio_codec);
  }
  return bits.join('  ·  ');
}

// The date picker as a sheet (13.3). Same generated day list as the desktop popover - it is
// capped to the rendered window and carries no typed day count (12.1).
function openDaySheet() {
  const todayKey = dayKey(new Date());
  const rows = getDayBoundariesInWindow().map(({ start }) => {
    const isToday = dayKey(start) === todayKey;
    const label = fmtDateTz(start);
    return `<button type="button" class="guide-sheet-row${isToday ? ' is-active' : ''}" ` +
      `data-jump="${start.toISOString()}"${isToday ? ' data-jump-now="1"' : ''}>` +
      `${escHtml(label)}${isToday ? '<span class="guide-sheet-row-note">Today</span>' : ''}</button>`;
  }).join('');

  const sheet = openSheet({ title: 'Jump to', body: rows });
  sheet.addEventListener('click', e => {
    const row = e.target.closest('[data-jump]');
    if (!row) return;
    if (row.dataset.jumpNow) scrollToNow();
    else jumpToInstant(new Date(row.dataset.jump));
    closeSheet();
    updateReadout();
  });
}

// 13.6's Layout sheet, in the order the section specifies: channel-column style, sort, grid
// behavior, then the program cell fields with their reorder arrows. Writes the same LAYOUT
// object and the same /api/user-prefs plumbing the desktop popover uses - only mobile's own
// pref key differs (12.3 as amended).
function openLayoutSheet() {
  const sheet = openSheet({ title: 'Layout', body: layoutSheetHtml() });

  const rerender = (full) => {
    saveLayout();
    if (full) sheet.querySelector('.modal-body').innerHTML = layoutSheetHtml();
    renderGuide();
  };

  // Delegated on the sheet, not bound per control: rebuilding the body for a reorder or a
  // sort change replaces every node inside it, and per-element listeners would survive
  // exactly one interaction (CLAUDE.md - the guide rebuilds its DOM, so bind by delegation).
  sheet.addEventListener('change', e => {
    const sel = e.target.closest('[data-sort-select]');
    if (sel) {
      if (sel.value !== sortKey) setSort(sel.value);
      rerender(true);
      return;
    }
    const cb = e.target.closest('input[data-layout]');
    if (!cb) return;
    LAYOUT[cb.dataset.layout] = cb.checked;
    if (cb.dataset.layout === 'show_failed') applyShowFailed();
    if (cb.dataset.layout === 'ch_logo_only') applyChannelColumnStyle();
    if (cb.dataset.layout === 'collapse_gaps') {
      const area = document.getElementById('guide-program-area');
      if (LAYOUT.collapse_gaps) { if (area) area.scrollLeft = 0; } else { scrollToNow(); }
    }
    rerender(false);
  });

  sheet.addEventListener('click', e => {
    const move = e.target.closest('[data-move]');
    if (move) {
      moveCellField(move.dataset.field, Number(move.dataset.move));
      rerender(true);
      return;
    }
    if (e.target.closest('[data-sort-dir]')) {
      setSort(sortKey);   // the active key flips direction (13.7, shared with desktop)
      rerender(true);
    }
  });
}

function layoutSheetHtml() {
  const spec = sortSpec();
  const cb = (key, label) =>
    `<label class="guide-sheet-cb"><input type="checkbox" data-layout="${key}"` +
    `${LAYOUT[key] ? ' checked' : ''}> ${escHtml(label)}</label>`;

  const order = cellFieldOrder();
  const fieldLabel = {
    rec_status: 'Recording status', title: 'Title', subtitle: 'Subtitle / episode',
    time: 'Time', tag_dots: 'Matched-tag dots',
  };
  const fieldRows = order.map((f, i) => {
    const fixed = f === 'title';
    const on = fixed || !!LAYOUT[f === 'time' ? 'start_time' : f];
    // `time` is the composite start/end/duration line, so its checkbox drives start_time -
    // the field the mobile default preset turns on (13.9).
    const key = f === 'time' ? 'start_time' : f;
    return `<div class="guide-sheet-fld">` +
      `<label class="guide-sheet-cb${fixed ? ' is-fixed' : ''}">` +
      `<input type="checkbox" ${fixed ? 'checked disabled' : `data-layout="${key}"${on ? ' checked' : ''}`}> ` +
      `${escHtml(fieldLabel[f])}${fixed ? ' <span class="guide-sheet-note">(always)</span>' : ''}</label>` +
      `<span class="guide-sheet-move">` +
      `<button type="button" data-move="-1" data-field="${f}"${i === 0 ? ' disabled' : ''} aria-label="Move up">▲</button>` +
      `<button type="button" data-move="1" data-field="${f}"${i === order.length - 1 ? ' disabled' : ''} aria-label="Move down">▼</button>` +
      `</span></div>`;
  }).join('');

  // The single-channel embed's sheet is the desktop popover's subset, for the same reason:
  // the channel column, the sort and "Include failed channels" all act on a list of
  // channels this card does not have. The hint says so rather than leaving their absence to
  // be noticed, and it is the one place the two sheets' copy diverges.
  const channelParts = SINGLE_CHANNEL ? '' :
    '<h4>Channel column</h4>' +
    cb('ch_logo_only', 'Logo only (narrower column)') +
    '<h4>Sort channels</h4>' +
    '<div class="guide-sheet-sort">' +
    `<select data-sort-select>${SORT_KEYS.map(s =>
      `<option value="${s.key}"${s.key === sortKey ? ' selected' : ''}>${escHtml(s.label)}</option>`).join('')}</select>` +
    `<button type="button" class="btn btn-sm" data-sort-dir>${escHtml(sortReversed ? spec.reversed : spec.natural)}</button>` +
    '</div>';

  return channelParts +
    '<h4>Grid behavior</h4>' +
    cb('collapse_gaps', 'Collapse gaps') +
    (SINGLE_CHANNEL ? '' : cb('show_failed', 'Include failed channels')) +
    '<h4>Program cell fields</h4>' + fieldRows +
    '<p class="guide-sheet-hint">Fields render in this order. Descriptions are not shown in ' +
    'grid cells at this width - tap a program to read one. Rows size to the tallest cell, ' +
    'so nothing clips.' +
    (SINGLE_CHANNEL ? ' The guide\'s channel-column, sort and "Include failed channels" ' +
      'settings are not here: this card is one known channel and shows no channel column.' : '') +
    '</p>';
}

function moveCellField(field, delta) {
  const order = cellFieldOrder().slice();
  const i = order.indexOf(field);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= order.length) return;
  order[i] = order[j];
  order[j] = field;
  LAYOUT.field_order = order;
}

// The Tags sheet drives the desktop panel's own inputs rather than keeping a second copy of
// the filter state: tagFilterState() reads those checkboxes, and it is the single predicate
// every consumer goes through (CLAUDE.md - adding a filter dimension means editing one
// place). A parallel mobile state would be exactly the drift that rule exists to stop.
function openTagsSheet() {
  const panel = document.getElementById('tag-filter-panel');
  if (!panel) return;

  const rowsFor = (cls) => Array.from(panel.querySelectorAll(`.${cls}`)).map(input => {
    const label = input.closest('.tag-filter-row');
    const dot = label ? label.querySelector('.tag-filter-dot') : null;
    return `<label class="guide-sheet-cb"><input type="checkbox" data-tag="${cls}" ` +
      `value="${escHtml(input.value)}"${input.checked ? ' checked' : ''}>` +
      (dot ? `<span class="tag-filter-dot" style="${escHtml(dot.getAttribute('style') || '')}"></span>` : '') +
      `${escHtml((label ? label.textContent : '').trim())}</label>`;
  }).join('');

  const mode = panel.querySelector('input[name="tag-filter-mode"]:checked');
  const body =
    '<h4>Include</h4>' +
    '<div class="guide-sheet-sort">' +
    ['any', 'all'].map(m =>
      `<label class="guide-sheet-cb"><input type="radio" name="sheet-tag-mode" value="${m}"` +
      `${mode && mode.value === m ? ' checked' : ''}> ${m === 'any' ? 'Any' : 'All'}</label>`).join('') +
    '</div>' +
    rowsFor('tag-filter-include') +
    '<h4>Exclude</h4>' + rowsFor('tag-filter-exclude') +
    '<p class="guide-sheet-hint">Exclude always beats include - a program carrying an ' +
    'excluded tag is hidden no matter what.</p>';

  const sheet = openSheet({ title: 'Tags', body });
  sheet.addEventListener('change', e => {
    const cb = e.target.closest('input[data-tag]');
    if (cb) {
      const target = panel.querySelector(`.${cb.dataset.tag}[value="${cb.value}"]`);
      if (target) target.checked = cb.checked;
    }
    const radio = e.target.closest('input[name="sheet-tag-mode"]');
    if (radio) {
      const target = panel.querySelector(`input[name="tag-filter-mode"][value="${radio.value}"]`);
      if (target) target.checked = true;
    }
    if (!cb && !radio) return;
    updateTagFilterSummary();
    renderGuide();
  });
}

// Same shape as openTagsSheet(): the sheet writes through to the desktop panel's own inputs
// (durationFilterState() reads those, and it is the single predicate every consumer goes
// through) rather than keeping a second copy of the filter state. Sheet inputs carry no id
// of their own - they are addressed by data-dur/data-dur-unit and mirrored onto the real
// #dur-filter-min/-max/-unit elements, since an id can't be duplicated in the DOM.
function openLengthSheet() {
  const minEl = document.getElementById('dur-filter-min');
  const maxEl = document.getElementById('dur-filter-max');
  const unitEl = document.getElementById('dur-filter-unit');
  if (!minEl || !maxEl || !unitEl) return;

  const body =
    '<div class="dur-filter-row"><span>Longer than</span>' +
    `<input type="number" min="0" inputmode="numeric" data-dur="min" value="${escHtml(minEl.value)}" placeholder="e.g. 3"></div>` +
    '<div class="dur-filter-row"><span>Shorter than</span>' +
    `<input type="number" min="0" inputmode="numeric" data-dur="max" value="${escHtml(maxEl.value)}" placeholder="e.g. 30"></div>` +
    '<div class="dur-filter-row"><span>Unit</span><select data-dur-unit>' +
    ['hours', 'minutes'].map(u => `<option value="${u}"${unitEl.value === u ? ' selected' : ''}>${u}</option>`).join('') +
    '</select></div>' +
    '<p class="guide-sheet-hint">Leave a box empty to not bound that side.</p>';

  // Clear stays in the footer (the bottom of the sheet, matching the desktop panel's own
  // Clear at the bottom of its dropdown) and deliberately does not close the sheet - same
  // reasoning as the desktop panel, which stays open after Clear so the reset is visible.
  const sheet = openSheet({
    title: 'Program length',
    body,
    footer: [{ label: 'Clear', class: 'btn btn-sm', onClick: () => {
      clearTimeout(timer);
      clearDurationFilter();
      sheet.querySelectorAll('[data-dur]').forEach(el => { el.value = ''; });
      const sel = sheet.querySelector('[data-dur-unit]');
      if (sel) sel.value = 'hours';
      return false;
    } }],
  });
  let timer = null;
  const debouncedApply = () => {
    clearTimeout(timer);
    timer = setTimeout(() => { updateDurFilterSummary(); renderGuide(); }, 200);
  };
  sheet.addEventListener('input', e => {
    const el = e.target.closest('input[data-dur]');
    if (!el) return;
    (el.dataset.dur === 'min' ? minEl : maxEl).value = el.value;
    debouncedApply();
  });
  sheet.addEventListener('change', e => {
    const sel = e.target.closest('select[data-dur-unit]');
    if (!sel) return;
    unitEl.value = sel.value;
    updateDurFilterSummary();
    renderGuide();
  });
}

// ── Modal ─────────────────────────────────────────────────────────────────────

const TERMINAL_STATUSES = new Set(['COMPLETED', 'FAILED', 'ABORTED', 'CONCATENATING',
                                  'ANALYZING', 'CONVERTING']);

// Base (unpadded) EPG program start/stop, set by openModal() for the currently-open
// new-recording modal - used to recompute padded times from scratch each time the
// profile dropdown changes, rather than cumulatively re-padding an already-padded value.
let _modalBaseStartIso = null;
let _modalBaseStopIso = null;
let _modalPaddingApplicable = false;

// Whether the user has hand-edited Start/Stop since the modal opened. A profile change
// must never overwrite a field the user already typed into (dev/docs/BUGS.md 2026-08-06).
let _modalStartEdited = false;
let _modalStopEdited = false;

// Mirrors app/url_utils.py::mask_creds (the canonical masker, also behind the mask_creds
// Jinja filter) so the same masking can be applied client-side to URLs populated purely
// from JSON. Keep the rules below in sync with that function.
function maskCreds(url) {
  if (!url) return url;
  // userinfo: scheme://user:pass@host
  url = url.replace(/^(https?:\/\/)[^/@\s]+@/i, '$1***:***@');
  // /live/, /movie/, /series/ <user>/<pass>/<id> path style (standard Xtream endpoints)
  url = url.replace(/(\/(?:live|movie|series)\/)([^/]+)\/([^/]+)(\/)/, '$1***/***$4');
  // username=/password= (or user=/pass=) query params
  url = url.replace(/((?:username|password|user|pass)=)[^&]+/gi, '$1***');
  // "Bare" <user>/<pass>/<id> path style with no /live/ prefix - common for
  // CDN-redirected Xtream stream URLs (scheme://host/<user>/<pass>/<id>[.ext]).
  url = url.replace(/^(https?:\/\/[^/]+\/)([^/]+)\/([^/]+)\/([^/]+?)(\?.*)?$/,
    (m, p1, p2, p3, p4, p5) => `${p1}***/***/${p4}${p5 || ''}`);
  return url;
}

function fallbackCopy(text, cb) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;opacity:0';
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
  cb();
}

function addSecondsToUtcIso(isoStr, seconds) {
  const d = new Date(isoStr + 'Z');
  d.setUTCSeconds(d.getUTCSeconds() + seconds);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}

/* Bumped on every openModal. A group note that arrives after the modal has been reopened
   on something else belongs to a target the user has already left, so it is dropped rather
   than painted - otherwise a slow fetch names the previous group over the current one. */
let _modalOpenToken = 0;

/* The group disclosure (#modal-group-note). A group-backed recording captures from ONE
   member, picked by format lock + health score at record-start time and re-picked on
   failover; this modal is where that is decided and was the one surface that never said so.

   Fetched per open rather than read off the row that opened the modal, because the answer
   is only true for this instant: app/recorder.py::start_recording re-resolves the member
   when the recording actually begins, so a recording scheduled now can start from a
   different feed than the one named here - which is exactly what the copy promises. */
async function showGroupNote(groupId, token) {
  const note = document.getElementById('modal-group-note');
  if (!note) return;
  if (!groupId) { note.style.display = 'none'; note.textContent = ''; return; }
  note.style.display = 'none';
  let data;
  try {
    data = await jsonFetch(`/api/channel-groups/${groupId}/record-context`);
  } catch (e) {
    // The name is the nice-to-have; the warning is the point. Losing the lookup must not
    // cost the user the disclosure that a group records from a member that can change.
    if (token !== _modalOpenToken) return;
    note.textContent = 'This is a channel group - the member it records from can change '
      + 'before and during the recording. That member could not be looked up just now.';
    note.style.display = '';
    return;
  }
  if (token !== _modalOpenToken) return;
  const gname = escHtml(data.group ? data.group.name : '');
  const parts = [];
  if (!data.serving) {
    parts.push(`Channel group "${gname}" has no recording-enabled member, so nothing `
      + "would record. Turn Recording on for a member on the group's page.");
  } else {
    const acct = data.serving.account_name
      ? ` (${escHtml(data.serving.account_name)})` : '';
    // Deliberately three short clauses rather than the full ranking rule. This sits above
    // every other field in the modal, and the long version ran to six lines at 375px.
    parts.push(`Channel group "${gname}" would record from `
      + `"${escHtml(data.serving.name)}"${acct} right now - its best-ranked available `
      + 'member. That can change before and during the recording.');
    if (data.format_override) {
      // The lock's zero-survivors override (DESIGN-channel-groups-model.md 15.2). Its
      // other three disclosures all land at or after record start; this is the only one
      // the user sees while not scheduling it is still an option.
      parts.push(`No member matches the locked ${escHtml(data.locked_format)}, so it will `
        + 'record off-format.');
    }
  }
  note.innerHTML = parts.join(' ');
  note.style.display = '';
}

function applyProfilePadding() {
  const note = document.getElementById('modal-padding-note');
  if (!_modalPaddingApplicable || !_modalBaseStartIso || !_modalBaseStopIso) {
    if (note) note.style.display = 'none';
    return;
  }
  const sel = document.getElementById('modal-profile');
  if (!sel) return;
  const prof = GUIDE_CONFIG.profiles[sel.value];
  const preMin  = prof ? prof.prePaddingMinutes  : 0;
  const postMin = prof ? prof.postPaddingMinutes : 0;

  // Never clobber a field the user has already typed into by hand - only the
  // untouched field gets recomputed from the program's own unpadded start/stop.
  if (!_modalStartEdited) {
    document.getElementById('modal-start').value =
      dateToTzInputValue(new Date(addSecondsToUtcIso(_modalBaseStartIso, -preMin * 60) + 'Z'), TZ);
  }
  if (!_modalStopEdited) {
    document.getElementById('modal-stop').value =
      dateToTzInputValue(new Date(addSecondsToUtcIso(_modalBaseStopIso, postMin * 60) + 'Z'), TZ);
  }

  if (note) {
    const paddingParts = [];
    if (!_modalStartEdited && preMin > 0) paddingParts.push(`starts ${preMin} min early`);
    if (!_modalStopEdited && postMin > 0) paddingParts.push(`ends ${postMin} min late`);
    const keptParts = [];
    if (_modalStartEdited) keptParts.push('start time');
    if (_modalStopEdited) keptParts.push('stop time');

    const sentences = [];
    if (paddingParts.length) {
      const profName = sel.options[sel.selectedIndex].text;
      sentences.push(`"${profName}" profile padding applied: ${paddingParts.join(' and ')}.`);
    }
    if (keptParts.length) {
      const verb = keptParts.length > 1 ? 'were' : 'was';
      sentences.push(`Your ${keptParts.join(' and ')} ${verb} left as entered - you already edited it.`);
    }
    if (sentences.length) {
      note.textContent = sentences.join(' ');
      note.style.display = '';
    } else {
      note.style.display = 'none';
    }
  }
}

function openModal(prog, ch, opts = {}) {
  const isActive = prog.has_recording && prog.recording_id &&
    (prog.recording_status === 'IN_PROGRESS' || prog.recording_status === 'PAUSED' ||
     prog.recording_status === 'RETRYING');

  if (isActive) {
    openActiveRecModal(prog);
    return;
  }

  // Replace mode ("Find Another Airing"): behaves like scheduling a new recording, but the
  // submit also deletes the SCHEDULED recording it replaces (replace_recording_id, handled
  // server-side in new_recording_json). Cleared on every open so it never leaks into a
  // normal schedule/edit.
  const replaceRecId = opts.replaceRecId || null;
  document.getElementById('modal-replace-id').value = replaceRecId || '';

  const isTerminal = prog.has_recording && prog.recording_id &&
    TERMINAL_STATUSES.has(prog.recording_status);

  const isEdit = prog.has_recording && prog.recording_id && !isTerminal;

  // A fresh open means neither field has been hand-edited yet.
  _modalStartEdited = false;
  _modalStopEdited = false;

  const startSrc = isEdit ? prog.recording_start_time : prog.start_time;
  const stopSrc  = isEdit ? prog.recording_stop_time  : prog.stop_time;
  const startET  = dateToTzInputValue(new Date(startSrc + 'Z'), TZ);
  const stopET   = dateToTzInputValue(new Date(stopSrc + 'Z'), TZ);

  document.getElementById('modal-name').value       = prog.suggested_name || prog.title;
  document.getElementById('modal-url').value        = prog.stream_url;
  const urlDisplay = document.getElementById('modal-url-display');
  urlDisplay.value = maskCreds(prog.stream_url);
  urlDisplay.dataset.revealed = 'false';
  document.getElementById('modal-url-toggle').textContent = 'Show';
  document.getElementById('modal-start').value      = startET;
  document.getElementById('modal-stop').value       = stopET;
  document.getElementById('modal-epg-id').value     = prog.id;
  // Prefer the program's own channel_id: a group row's ch.id is the synthetic
  // 'g<id>' DOM key, while prog.channel_id is the group's real active member.
  document.getElementById('modal-channel-id').value = prog.channel_id || ((ch && ch.id) ? ch.id : '');
  document.getElementById('modal-group-id').value   = prog.group_id || '';

  // Every surface that opens this modal passes group_id through, so the one call here is
  // the whole disclosure - the guide, the EPG search page, the dashboard's edit action and
  // the recording detail page all get it without knowing it exists (dev/changelog/904).
  _modalOpenToken += 1;
  showGroupNote(prog.group_id, _modalOpenToken);

  const profileSel = document.getElementById('modal-profile');
  if (profileSel) {
    if (isEdit) {
      _modalPaddingApplicable = false;
      profileSel.value = (prog.recording_profile_id != null) ? String(prog.recording_profile_id) : '';
      const note = document.getElementById('modal-padding-note');
      if (note) note.style.display = 'none';
    } else {
      _modalBaseStartIso = prog.start_time;
      _modalBaseStopIso  = prog.stop_time;
      _modalPaddingApplicable = true;
      const defaultProfileId = (ch && ch.default_profile_id != null) ? String(ch.default_profile_id) : '';
      profileSel.value = GUIDE_CONFIG.profiles[defaultProfileId] ? defaultProfileId : '';
      applyProfilePadding();
    }
  }

  const hint = document.getElementById('modal-name-hint');
  if (hint) hint.textContent = prog.description ? prog.description.slice(0, 120) : '';

  const title          = document.getElementById('modal-title');
  const submit         = document.getElementById('modal-submit');
  const form           = document.getElementById('modal-form');
  const deleteBtn      = document.getElementById('modal-delete');
  const detailLinkDiv  = document.getElementById('modal-detail-link');
  const detailAnchor   = document.getElementById('modal-detail-anchor');

  if (replaceRecId) {
    title.textContent        = 'Replace recording';
    submit.textContent       = 'Replace recording';
    form.action              = GUIDE_CONFIG.newRecordingUrl + '-json';
    deleteBtn.style.display  = 'none';
    detailLinkDiv.style.display = 'none';
  } else if (isEdit) {
    title.textContent        = 'Edit recording';
    submit.textContent       = 'Save';
    form.action              = GUIDE_CONFIG.editRecordingUrlBase + prog.recording_id + '/edit-json';
    deleteBtn.style.display  = 'inline-block';
    deleteBtn.dataset.recId  = prog.recording_id;
    detailLinkDiv.style.display = 'none';
  } else if (isTerminal) {
    title.textContent        = 'Schedule new recording';
    submit.textContent       = 'Schedule recording';
    form.action              = GUIDE_CONFIG.newRecordingUrl + '-json';
    deleteBtn.style.display  = 'none';
    detailLinkDiv.style.display = 'block';
    detailAnchor.href        = GUIDE_CONFIG.recDetailUrlBase + prog.recording_id;
  } else {
    title.textContent        = 'Schedule recording';
    submit.textContent       = 'Schedule recording';
    form.action              = GUIDE_CONFIG.newRecordingUrl + '-json';
    deleteBtn.style.display  = 'none';
    detailLinkDiv.style.display = 'none';
  }

  document.getElementById('record-modal').style.display = 'flex';
  syncScrollLock();
}

function showModalError(msg, conflicts) {
  showConflictError('modal-error', msg, conflicts, GUIDE_CONFIG.recDetailUrlBase);
}

// Shared by the initial submit and by "Proceed anyway" - a schedule that overlaps another
// recording answers {success: false, overlap_warning, connection_limit_warning} rather than
// failing outright (dev/changelog/858); this is what turns that into a warning-with-a-way-past
// rather than the caller silently treating an unconfirmed response as success.
async function submitModalForm(form, data) {
  const isReplace = document.getElementById('modal-replace-id').value !== '';
  try {
    const resp = await jsonFetch(form.action, { method: 'POST', body: data });
    if (resp && resp.success === false) {
      showModalWarnings(resp, data);
      return;
    }
    if (isReplace && resp && resp.id) {
      window.location.href = GUIDE_CONFIG.recDetailUrlBase + resp.id;
      return;
    }
    closeModal();
    fetchAndRender();
    // Pages with no guide grid (fetchAndRender is a no-op there) set this to refresh
    // their own display after a schedule/edit save - see index.html, recording_detail.html.
    if (typeof window.onScheduleSaved === 'function') window.onScheduleSaved();
  } catch (e) {
    showModalError(e.message || 'Request failed. Check your connection.', e.data && e.data.conflicts);
  }
}

function showModalWarnings(data, formData) {
  const el = document.getElementById('modal-error');
  el.innerHTML = recordingWarningsHtml(data, GUIDE_CONFIG.recDetailUrlBase) +
    '<div style="margin-top:0.5rem;"><button type="button" class="btn btn-sm btn-danger-outline" ' +
    'id="modal-warn-force">Proceed anyway</button></div>';
  el.style.display = 'block';
  document.getElementById('modal-warn-force').addEventListener('click', () => {
    const forced = new URLSearchParams(formData);
    forced.set('force', '1');
    submitModalForm(document.getElementById('modal-form'), forced);
  });
}

function closeModal() {
  document.getElementById('record-modal').style.display = 'none';
  document.getElementById('modal-error').style.display = 'none';
  syncScrollLock();
}

// ── Active Recording Modal ────────────────────────────────────────────────────

let _activeRecId = null;

function openActiveRecModal(prog) {
  _activeRecId = prog.recording_id;
  const isPaused = prog.recording_status === 'PAUSED';
  const isRetrying = prog.recording_status === 'RETRYING';

  document.getElementById('active-rec-title').textContent =
    isPaused ? 'Recording paused' : isRetrying ? 'Waiting to retry' : 'Recording in progress';
  document.getElementById('active-rec-name').textContent = prog.title +
    (prog.channel_name ? '  -  ' + prog.channel_name : '');

  const stopET = dateToTzInputValue(new Date(prog.recording_stop_time + 'Z'), TZ);
  document.getElementById('active-rec-stop').value = stopET;

  document.getElementById('active-rec-detail-anchor').href =
    GUIDE_CONFIG.recDetailUrlBase + prog.recording_id;

  // Pause/Resume is a manual action with no meaning while RETRYING - there is no active
  // capture to pause, and resuming happens automatically on its own backoff schedule (or via
  // Stop/Cancel below to end the wait early). Hide it rather than wiring a third button state.
  const pauseBtn = document.getElementById('active-rec-pause-btn');
  pauseBtn.style.display = isRetrying ? 'none' : '';
  pauseBtn.textContent = isPaused ? 'Resume' : 'Pause';
  pauseBtn.dataset.paused = isPaused ? '1' : '0';

  document.getElementById('active-rec-error').style.display = 'none';
  document.getElementById('active-rec-modal').style.display = 'flex';
  syncScrollLock();
}

function closeActiveRecModal() {
  document.getElementById('active-rec-modal').style.display = 'none';
  _activeRecId = null;
  syncScrollLock();
}

function showActiveRecError(msg) {
  const el = document.getElementById('active-rec-error');
  el.textContent = msg;
  el.style.display = 'block';
}

async function activeRecAction(path, confirmMsg) {
  if (confirmMsg && !confirm(confirmMsg)) return;
  const recId = _activeRecId;
  if (!recId) return;
  try {
    await jsonFetch('/recordings/' + recId + path, { method: 'POST' });
    closeActiveRecModal();
    fetchAndRender();
  } catch (e) {
    showActiveRecError(e.message || 'Request failed. Check your connection.');
  }
}

// ── Search / instant filter ───────────────────────────────────────────────────

// Split a query into {positives, negatives} (lowercased). A '-x' token (len>1)
// excludes 'x'; bare '-' and empties are ignored. Mirrors the backend
// _parse_search_terms in app/routes/guide.py - keep the two in sync.
function parseSearchTerms(q) {
  const positives = [], negatives = [];
  q.toLowerCase().split(/\s+/).filter(Boolean).forEach(tok => {
    if (tok.startsWith('-') && tok.length > 1) negatives.push(tok.slice(1));
    else if (tok !== '-') positives.push(tok);
  });
  return { positives, negatives };
}

function wordsMatch(terms, ...fields) {
  const combined = fields.join(' ').toLowerCase();
  return terms.positives.every(w => combined.includes(w)) &&
         terms.negatives.every(w => !combined.includes(w));
}

// THE match predicate for a single program: text terms AND the tag filter AND the duration
// filter must all pass. Every consumer goes through this one function - the cell dimmer, the
// collapse-gaps segment union and the row-height calculation - because a filter dimension
// added to only some consumers is a defect this page has shipped twice (the health filter,
// then the tag filter, were each missed by computeVisibleSegments in turn). Adding a
// dimension means adding it here, once.
//
// tagState/durState are hoisted by callers: they read the DOM, so computing them per program
// in a loop over ~1000 programs is pure waste.
function programMatches(prog, terms, tagState, durState) {
  const state = tagState || tagFilterState();
  const dur = durState || durationFilterState();
  const hasTerms = !!terms && (terms.positives.length > 0 || terms.negatives.length > 0);
  if (hasTerms && !wordsMatch(terms, prog.title, prog.sub_title || '', prog.description || '')) {
    return false;
  }
  const tagActive = state.includeIds.length > 0 || state.excludeIds.length > 0;
  if (tagActive && !programPassesTagFilter(prog, state)) return false;
  return programPassesDurationFilter(prog, dur);
}

// Whether a channel's ROW is shown at all: its name matches, or any of its programs matches
// the text terms. Deliberately text-only, matching the behaviour this page has always had - a
// tag filter dims non-matching cells and collapses gaps, it does not hide channel rows.
function channelMatches(ch, terms) {
  const hasTerms = !!terms && (terms.positives.length > 0 || terms.negatives.length > 0);
  if (!hasTerms) return true;
  return wordsMatch(terms, ch.name) ||
    (ch.programs || []).some(p => wordsMatch(terms, p.title, p.sub_title || '', p.description || ''));
}

function applySearch(q) {
  const terms = parseSearchTerms(q);
  const hasTerms = terms.positives.length > 0 || terms.negatives.length > 0;
  const tagState = tagFilterState();
  const tagActive = tagState.includeIds.length > 0 || tagState.excludeIds.length > 0;
  const durState = durationFilterState();
  const durActive = durationFilterActive(durState);

  channelData.forEach(ch => {
    const row = channelRowFor(ch.id);
    const pr = programRowFor(ch.id);
    const visible = channelMatches(ch, terms);
    if (row) row.style.display = visible ? '' : 'none';
    if (!pr) return;

    pr.classList.toggle('hidden-row', !visible);
    if ((hasTerms || tagActive || durActive) && visible) {
      // Cells carry their program's index, so the dimmer resolves the program object
      // directly instead of matching on rendered title text (which broke for two programs
      // sharing a title, and for any cell whose title field was truncated).
      pr.querySelectorAll('.guide-program').forEach(el => {
        const prog = (ch.programs || [])[Number(el.dataset.progIdx)];
        if (prog) el.style.opacity = programMatches(prog, terms, tagState, durState) ? '1' : '0.06';
      });
    } else {
      pr.querySelectorAll('.guide-program').forEach(el => { el.style.opacity = ''; });
    }
  });

  updateNowLine();
}

// ── Collapse gaps (parallel render path, active only when the setting is on && a query is set) ────

function hasActiveQuery() {
  const el = document.getElementById('guide-search');
  return !!el && el.value.trim().length > 0;
}

function isCollapseActive() {
  return LAYOUT.collapse_gaps && (hasActiveQuery() || tagFilterActive() || durationFilterActive());
}

// Mirrors applyHealthFilter()'s own hide/warn/normal logic exactly, so the two can never diverge:
// a channel is truly hidden (channel-health-failed + hide-failed-channels) only when it's FAILED,
// has no active/scheduled recording (that combination gets channel-health-warn instead, which is
// never hidden), and "Include failed channels" is currently off.
function isChannelHealthHidden(ch) {
  // You are on that channel's own page - hiding it is never the answer (see applyShowFailed).
  if (SINGLE_CHANNEL) return false;
  if (ch.last_test_status !== 'FAILED') return false;
  const hasActiveRec = (ch.recordings || []).some(r =>
    r.status === 'SCHEDULED' || r.status === 'IN_PROGRESS' || r.status === 'RETRYING');
  if (hasActiveRec) return false;
  return !LAYOUT.show_failed;
}

// Union of every matching program's time span across all channels, merged into contiguous
// segments. A program counts as "matching" here under the same combined rule the uncollapsed
// render uses (applySearch): text words (if any) AND the active tag filter (if any) must both
// pass - so a tags-only filter (no search text) collapses gaps just like a text-only search does.
// Returns null (meaning "fall back to the normal uncollapsed render") when nothing matches by
// program - e.g. only channel-name matches exist, which have no associated time span.
// Channels currently hidden by the "Show Failed" health filter are excluded from the union so a
// hidden channel's match can't force the shared timeline open around empty space.
function computeVisibleSegments(terms) {
  const intervals = [];
  const tagState = tagFilterState();
  const durState = durationFilterState();
  channelData.forEach(ch => {
    if (isChannelHealthHidden(ch)) return;
    (ch.programs || []).forEach(p => {
      if (!programMatches(p, terms, tagState, durState)) return;
      const s = new Date(p.start_time + 'Z');
      const e = new Date(p.stop_time  + 'Z');
      const cs = s < windowStart ? windowStart : s;
      const ce = e > windowEnd   ? windowEnd   : e;
      if (ce > cs) intervals.push([cs, ce]);
    });
  });

  if (!intervals.length) return null;

  intervals.sort((a, b) => a[0] - b[0]);
  const segments = [];
  let curStart = intervals[0][0];
  let curEnd   = intervals[0][1];
  for (let i = 1; i < intervals.length; i++) {
    const [s, e] = intervals[i];
    if (s <= curEnd) {
      if (e > curEnd) curEnd = e;
    } else {
      segments.push({ start: curStart, end: curEnd });
      curStart = s;
      curEnd = e;
    }
  }
  segments.push({ start: curStart, end: curEnd });
  return segments;
}

// Assigns each segment a leftPx/widthPx on the compressed axis (segment width = real duration,
// same PIXELS_PER_MINUTE scale; a fixed CUT_WIDTH_PX gap between consecutive segments).
function buildCollapsedLayout(segments) {
  // A skipped LEADING gap is a gap and gets the same marker: when the first match starts
  // after the window does, the grid opens with a sawtooth rather than silently pretending
  // the window began there (DESIGN.md 12.6).
  leadingCut = segments.length > 0 && segments[0].start > windowStart;
  let cursor = leadingCut ? CUT_WIDTH_PX : 0;
  collapsedSegments = segments.map((seg, i) => {
    if (i > 0) cursor += CUT_WIDTH_PX;
    const widthPx = ((seg.end - seg.start) / 60000) * PIXELS_PER_MINUTE;
    const out = { start: seg.start, end: seg.end, leftPx: cursor, widthPx };
    cursor += widthPx;
    return out;
  });
  return cursor;
}

function collapsedTotalWidth() {
  if (!collapsedSegments.length) return 0;
  const last = collapsedSegments[collapsedSegments.length - 1];
  return last.leftPx + last.widthPx;
}

// Empty spacer matching a .guide-collapse-cut divider's width, so the day/time header flex
// flow stays aligned with the absolutely-positioned program rows in collapse mode.
function cutSpacer() {
  const spacer = document.createElement('div');
  spacer.className = 'guide-collapse-cut-spacer';
  spacer.style.width = CUT_WIDTH_PX + 'px';
  return spacer;
}

// Clamps [start, stop) to a single collapsed segment's real-time range; null if no overlap.
function segmentOverlap(start, stop, seg) {
  const s = Math.max(start.getTime(), seg.start.getTime());
  const e = Math.min(stop.getTime(), seg.end.getTime());
  if (e <= s) return null;
  return { clStart: new Date(s), clEnd: new Date(e) };
}

function renderDayHeaderCollapsed() {
  const bar = document.getElementById('guide-day-header');
  if (!bar) return;
  bar.innerHTML = '';
  bar.style.width = collapsedTotalWidth() + 'px';

  if (leadingCut) bar.appendChild(cutSpacer());
  collapsedSegments.forEach((seg, i) => {
    if (i > 0) bar.appendChild(cutSpacer());
    getDayBoundariesInRange(seg.start, seg.end).forEach(({ start, end }) => {
      const widthPx = ((end - start) / 60000) * PIXELS_PER_MINUTE;
      const dseg = document.createElement('div');
      dseg.style.width = widthPx + 'px';

      const full = fmtDateTz(start);
      const isNarrow = widthPx < 150;
      dseg.className = 'guide-day-segment' + (isNarrow ? ' narrow' : '');
      dseg.title = full;

      const label = document.createElement('span');
      label.className = 'guide-day-segment-label';
      label.textContent = isNarrow
        ? fmtDateTz(start, { weekday: 'short', month: 'short', day: 'numeric' })
        : full;
      dseg.appendChild(label);

      bar.appendChild(dseg);
    });
  });
}

function renderTimeHeaderCollapsed() {
  const header = document.getElementById('guide-time-header');
  if (!header) return;
  header.innerHTML = '';
  header.style.width = collapsedTotalWidth() + 'px';

  if (leadingCut) header.appendChild(cutSpacer());
  collapsedSegments.forEach((seg, i) => {
    if (i > 0) header.appendChild(cutSpacer());
    const segMinutes = (seg.end - seg.start) / 60000;
    for (let m = 0; m < segMinutes; m += SLOT_MINUTES) {
      const slotMinutes = Math.min(SLOT_MINUTES, segMinutes - m);
      const slotDate = new Date(seg.start.getTime() + m * 60000);
      const slot = document.createElement('div');
      slot.style.width = (slotMinutes * PIXELS_PER_MINUTE) + 'px';
      slot.className = 'guide-time-slot';
      slot.textContent = fmtTimeTz(slotDate);
      header.appendChild(slot);
    }
  });
}

function renderProgramRowsCollapsed(terms) {
  const totalPx = collapsedTotalWidth();
  const tagState = tagFilterState();
  const durState = durationFilterState();

  channelData.forEach(ch => {
    const row = programRowFor(ch.id);
    if (!row) return;
    row.innerHTML = '';
    row.style.width = totalPx + 'px';

    const sidebarRow = channelRowFor(ch.id);
    const visible = channelMatches(ch, terms);
    if (sidebarRow) sidebarRow.style.display = visible ? '' : 'none';
    row.classList.toggle('hidden-row', !visible);
    if (!visible) return;

    (ch.recordings || []).forEach(rec => {
      const recStart = new Date(rec.start_time + 'Z');
      const recStop  = new Date(rec.stop_time  + 'Z');
      collapsedSegments.forEach(seg => {
        const ov = segmentOverlap(recStart, recStop, seg);
        if (!ov) return;
        const leftPx  = seg.leftPx + ((ov.clStart - seg.start) / 60000) * PIXELS_PER_MINUTE;
        const widthPx = ((ov.clEnd - ov.clStart) / 60000) * PIXELS_PER_MINUTE;
        if (widthPx <= 0) return;

        const bandStatusClass = ' ' + recCssClass(rec.status);
        const band = document.createElement('div');
        band.className = 'guide-recording-band' + bandStatusClass;
        band.style.left  = leftPx + 'px';
        band.style.width = widthPx + 'px';
        row.appendChild(band);

        const outline = document.createElement('div');
        outline.className = 'guide-recording-outline' + bandStatusClass;
        outline.style.left  = leftPx + 'px';
        outline.style.width = widthPx + 'px';
        row.appendChild(outline);
      });
    });

    (ch.programs || []).forEach((prog, progIdx) => {
      const pStart = new Date(prog.start_time + 'Z');
      const pStop  = new Date(prog.stop_time  + 'Z');
      const matches = programMatches(prog, terms, tagState, durState);

      collapsedSegments.forEach(seg => {
        const ov = segmentOverlap(pStart, pStop, seg);
        if (!ov) return;

        const leftPx  = seg.leftPx + ((ov.clStart - seg.start) / 60000) * PIXELS_PER_MINUTE;
        const widthPx = Math.max(2, ((ov.clEnd - ov.clStart) / 60000) * PIXELS_PER_MINUTE - 2);

        const el = document.createElement('div');
        const progRecClass = prog.has_recording ? (' has-recording ' + recCssClass(prog.recording_status)) : '';
        el.className = 'guide-program' + progRecClass + (prog.is_dummy ? ' is-dummy' : '');
        if (widthPx < 60) el.classList.add('narrow');
        el.style.left  = leftPx + 'px';
        el.style.width = widthPx + 'px';
        el.dataset.progIdx = progIdx;
        el.style.opacity = matches ? '1' : '0.06';

        if (prog.has_recording && prog.recording_start_time) {
          const recStart = new Date(prog.recording_start_time + 'Z');
          const recStop  = new Date(prog.recording_stop_time  + 'Z');
          const overlapStart = Math.max(pStart.getTime(), recStart.getTime());
          const overlapEnd   = Math.min(pStop.getTime(),  recStop.getTime());
          const elemStart    = ov.clStart.getTime();
          const elemEnd      = ov.clEnd.getTime();
          const elemDuration = elemEnd - elemStart;
          if (elemDuration > 0) {
            const leftPct  = ((Math.max(overlapStart, elemStart) - elemStart) / elemDuration * 100).toFixed(2);
            const rightPct = ((Math.min(overlapEnd,   elemEnd)   - elemStart) / elemDuration * 100).toFixed(2);
            el.style.setProperty('--rec-left',  leftPct  + '%');
            el.style.setProperty('--rec-right', rightPct + '%');
          }
        }

        el.innerHTML = programCellHtml(prog, ch);

        el.addEventListener('click', () => openProgramTarget(prog, ch));
        row.appendChild(el);
      });
    });
  });
}

function renderMidnightLinesCollapsed() {
  document.querySelectorAll('.guide-midnight-line').forEach(el => el.remove());
  const area = document.getElementById('guide-program-area');
  if (!area) return;

  collapsedSegments.forEach(seg => {
    const boundaries = getDayBoundariesInRange(seg.start, seg.end);
    for (let i = 0; i < boundaries.length - 1; i++) {
      const midnight = boundaries[i].end;
      const offsetMin = (midnight - seg.start) / 60000;
      const line = document.createElement('div');
      line.className = 'guide-midnight-line';
      line.style.left = (seg.leftPx + offsetMin * PIXELS_PER_MINUTE) + 'px';
      area.appendChild(line);
    }
  });
}

function renderCollapseCuts() {
  document.querySelectorAll('.guide-collapse-cut').forEach(el => el.remove());
  const area = document.getElementById('guide-program-area');
  if (!area) return;
  const addCut = leftPx => {
    const cut = document.createElement('div');
    cut.className = 'guide-collapse-cut';
    cut.style.left = leftPx + 'px';
    area.appendChild(cut);
  };
  if (leadingCut) addCut(0);
  for (let i = 1; i < collapsedSegments.length; i++) {
    addCut(collapsedSegments[i].leftPx - CUT_WIDTH_PX);
  }
}

function updateNavAvailability() {
  // Scrolling through real time is meaningless while the timeline is a compressed set of
  // matching blocks, so the whole segmented control goes disabled.
  const disable = isCollapseActive();
  document.querySelectorAll('#guide-timenav button').forEach(btn => { btn.disabled = disable; });
}

// 12.6: filtering NEVER destroys grid DOM. The empty state is a sibling that is shown while
// the grid is hidden, so clearing the filter always restores the guide - rebuilding the grid's
// innerHTML to render it would permanently destroy the nodes the renderer holds references to.
function showEmptyState(isEmpty) {
  const head = document.getElementById('guide-head');
  const body = document.getElementById('guide-wrap');
  const empty = document.getElementById('guide-empty');
  if (head) head.hidden = isEmpty;
  if (body) body.hidden = isEmpty;
  if (empty) empty.hidden = !isEmpty;
}

// What the active filter is, in words. The counts alone cannot distinguish "this channel
// only has four programs tonight" from "a filter is hiding the rest", which is the whole
// reason the line exists.
function filterModeText(terms) {
  const bits = [];
  if (terms.positives.length) bits.push(`text "${terms.positives.join(' ')}"`);
  if (terms.negatives.length) bits.push(`excluding "${terms.negatives.join(' ')}"`);
  const { includeIds, excludeIds, mode } = tagFilterState();
  if (includeIds.length) bits.push(`${mode} of ${includeIds.length} tag${includeIds.length === 1 ? '' : 's'}`);
  if (excludeIds.length) bits.push(`${excludeIds.length} tag${excludeIds.length === 1 ? '' : 's'} excluded`);
  const durState = durationFilterState();
  if (durationFilterActive(durState)) bits.push(durationFilterLabel(durState));
  if (isCollapseActive()) bits.push('gaps collapsed');
  return bits.length ? bits.join(' · ') : 'no filter';
}

// The colour key for the matched-tag dots on program cells. Only the tags actually on a
// visible program are listed, and only while the dots are switched on - a key for a colour
// that is not on screen is noise. On the guide the account legend does this job (channel
// rows carry account edge-colours); a single-channel card has one account and tag dots
// instead, so it gets a tag key rather than a legend of one.
function renderTagKey(terms) {
  const host = document.getElementById('guide-sb-tagkey');
  if (!host) return;
  if (!LAYOUT.tag_dots) { host.innerHTML = ''; return; }
  const tagState = tagFilterState();
  const durState = durationFilterState();
  const seen = new Map();
  channelData.forEach(ch => {
    if (isChannelHealthHidden(ch)) return;
    (ch.programs || []).forEach(p => {
      if (!programMatches(p, terms, tagState, durState)) return;
      (p.matched_tags || []).forEach(t => { if (!seen.has(t.name)) seen.set(t.name, t.color); });
    });
  });
  host.innerHTML = Array.from(seen.entries()).map(([name, color]) =>
    `<span class="guide-legend-item"><span class="guide-legend-dot" style="background: ${escHtml(color)}"></span>` +
    `${escHtml(name)}</span>`).join('');
}

// Status bar counts (12.6 as amended - the account legend beside them is server-rendered,
// since accounts do not change while the page is open).
function updateStatusBar(shownChannels, shownPrograms, terms) {
  const el = document.getElementById('guide-sb-counts');
  renderTagKey(terms);
  if (!el) return;
  // A single-channel card counts programs, not channels: "1 of 1 channels" says nothing.
  if (SINGLE_CHANNEL) {
    const total = channelData.reduce((n, ch) => n + (ch.programs || []).length, 0);
    el.textContent = `${shownPrograms} of ${total} program${total === 1 ? '' : 's'}  ·  ${filterModeText(terms)}`;
    return;
  }
  const total = channelData.length;
  const chanPart = shownChannels === total
    ? `${total} channel${total === 1 ? '' : 's'}`
    : `${shownChannels} of ${total} channels`;
  el.textContent = `${chanPart}  ·  ${shownPrograms} program${shownPrograms === 1 ? '' : 's'} shown`;
}

// Single dispatcher for the whole render pipeline.
function renderGuide() {
  const searchInput = document.getElementById('guide-search');
  const q = searchInput ? searchInput.value.trim() : '';
  const terms = parseSearchTerms(q);
  const hasTerms = terms.positives.length > 0 || terms.negatives.length > 0;
  const segments = (LAYOUT.collapse_gaps && (hasTerms || tagFilterActive() || durationFilterActive())) ? computeVisibleSegments(terms) : null;

  // Rows size to the tallest cell either side produces, so this has to be settled before
  // anything is positioned inside a row (12.5).
  document.documentElement.style.setProperty('--guide-rowh', `${computeRowHeight(terms)}px`);

  if (segments) {
    buildCollapsedLayout(segments);
    renderDayHeaderCollapsed();
    renderTimeHeaderCollapsed();
    renderProgramRowsCollapsed(terms);
    renderMidnightLinesCollapsed();
    renderCollapseCuts();
    document.querySelectorAll('.guide-now-line').forEach(el => el.remove());
  } else {
    document.querySelectorAll('.guide-collapse-cut').forEach(el => el.remove());
    leadingCut = false;
    renderDayHeader();
    renderTimeHeader();
    renderProgramRows();
    renderMidnightLines();
    applySearch(q);
    updateNowLine();
  }

  renderChannelColumn();
  applyHealthFilter();
  applySort();
  updateNavAvailability();

  const tagState = tagFilterState();
  const durState = durationFilterState();
  const shown = channelData.filter(ch => channelMatches(ch, terms) && !isChannelHealthHidden(ch));
  const shownPrograms = shown.reduce((n, ch) =>
    n + (ch.programs || []).filter(p => programMatches(p, terms, tagState, durState)).length, 0);
  // A channel whose NAME matched still has a row worth showing even if none of its programs
  // matched, so that case is not the empty state.
  const nameOnlyMatch = hasTerms && shown.some(ch => wordsMatch(terms, ch.name));
  showEmptyState((hasTerms || tagFilterActive() || durationFilterActive(durState)) && shownPrograms === 0 && !nameOnlyMatch);
  updateStatusBar(shown.length, shownPrograms, terms);
  updateReadout();
  syncHeadTrack();
  stripGridTooltips();
}

// 13.1: nothing on the mobile guide may carry information reachable only by hover, and a
// tap fires an emulated mouseover that would pop util.js's delegated tooltip anyway. Every
// one of these facts is in the program or channel sheet instead.
//
// Done as one sweep at the end of the render rather than gated at each of the eight
// emission sites (recStatusBadge, tagBadgesHtml, the four channel-column renderers, the
// health-fail badge, and the two server-rendered tips the template writes): a gate is one
// more thing a future renderer can forget, and this cannot be forgotten.
function stripGridTooltips() {
  if (!isMobileGuide()) return;
  document.querySelectorAll(
    '#guide-channel-col [data-tip], #guide-program-rows [data-tip]').forEach(el => {
    el.removeAttribute('data-tip');
    el.classList.remove('tip-plain');
  });
}

// ── Quality dots & badges ─────────────────────────────────────────────────────

// ── Channel column (DESIGN.md 12.4) ─────────────────────────────────────────

// Everything the channel column shows that comes from the EPG payload rather than the
// server-rendered markup: the numeric health badge, the tech readout on line two, the DUP
// badge and the group badge's member list.
function renderChannelColumn() {
  channelData.forEach(ch => {
    if (ch.is_group) renderGroupBadge(ch);
    renderLifecycleBadge(ch);
    renderDupBadge(ch);
    renderHealthBadge(ch);
    renderTechReadout(ch);
  });
}

// Provider-removed channels (dev/changelog/626, 627): the guide previously showed a channel the
// provider stopped sending exactly like a healthy one. Mirrors the 'missing' marker /channels
// and the EPG deep search already have (app/accounts.py::channel_lifecycle_state) - same
// derived state, rendered here since the guide's channel column is a separate surface from
// the channel-search engine those two go through.
function renderLifecycleBadge(ch) {
  const badge = document.querySelector(`.guide-lifecycle-badge[data-channel-id="${ch.id}"]`);
  if (!badge) return;
  const isMissing = ch.lifecycle === 'missing';
  badge.innerHTML = isMissing
    ? `<span class="qbadge qbadge-warn tip-plain" data-tip="${escTipAttr(
        `No longer seen in ${ch.account_name || 'this account'}'s synced feed since ${ch.lifecycle_date}.\n` +
        'Still shown here because it is in your TV Guide.' + repointHint(ch, false))}">Missing</span>`
    : '';
  // Dims the row's other content AND the program cells alongside it (.guide-row) - the pill
  // itself is excluded from the dimming in CSS (.guide-lifecycle-badge is the one thing that
  // must stay full-strength, or the alert it's carrying gets lost in the dimming meant to
  // surface it).
  const row     = document.querySelector(`.guide-channel-row[data-channel-id="${ch.id}"]`);
  const progRow = document.querySelector(`.guide-row[data-channel-id="${ch.id}"]`);
  [row, progRow].forEach(el => { if (el) el.classList.toggle('is-removed', isMissing); });
}

// Shared by the "Missing" pill's tooltip and the mobile channel sheet's Status line
// (openChannelSheet) so the two surfaces never disagree about whether a re-point recovery
// exists - both read the same server-computed lifecycle_repoint_available (dev/changelog/627),
// never re-derive it client-side. Worded per surface: the sheet already has its own "Open
// channel" button, desktop has to name the click target itself.
function repointHint(ch, sheet) {
  if (!ch.lifecycle_repoint_available) return '';
  return sheet
    ? ' It is also a duplicate stream URL - tap Open channel below to re-point it to the duplicate.'
    : '\nIt is also a duplicate stream URL - click the channel name to open its page and re-point it to the duplicate.';
}

function renderGroupBadge(ch) {
  const badge = document.querySelector(`.guide-group-badge[data-channel-id="${ch.id}"]`);
  if (!badge) return;
  const memberLines = (ch.members || []).map((m, i) =>
    `${i + 1}. ${m.name} (${m.account_name}) - ★${m.effective_score}${m.scored ? '' : ' (untested)'}`);
  badge.dataset.tip = escTipAttr(
    `Channel group - records from the best of ${ch.member_count || 0} feeds\n` +
    `Active: ${ch.active_channel_name || '-'}` +
    (memberLines.length ? `\n${memberLines.join('\n')}` : ''));
}

function renderDupBadge(ch) {
  const row = document.querySelector(`.guide-dup-badges[data-channel-id="${ch.id}"]`);
  if (!row) return;
  row.innerHTML = ch.duplicate_title
    ? `<span class="qbadge qbadge-warn tip-plain" data-tip="${escTipAttr(ch.duplicate_title)}">DUP</span>`
    : '';
}

// The number and the colour both come from effectiveHealthScore(), so they cannot disagree.
// A channel that has never been tested shows `--` in the distinct unknown treatment - never a
// fabricated score, and never styled as a bad one (12.4).
function renderHealthBadge(ch) {
  const state = healthState(ch);

  // 13.5's mobile carrier for the same state. Coloured from the same healthState() call as
  // the badge, so the dot and the number can no more disagree than the badge's colour and
  // its own number can. Hidden by CSS on desktop.
  const dot = document.querySelector(`.guide-health-dot[data-channel-id="${ch.id}"]`);
  if (dot) dot.className = `guide-health-dot hb-${state === 'untested' ? 'none' : state}`;

  const badge = document.querySelector(`.guide-health-badge[data-channel-id="${ch.id}"]`);
  if (!badge) return;
  const score = effectiveHealthScore(ch);
  badge.className = `guide-health-badge hb-${state === 'untested' ? 'none' : state} tip-plain`;

  if (state === 'untested') {
    badge.textContent = '--';
    badge.dataset.tip = escTipAttr(
      'Never tested\nThis channel has no health score yet. Run a health check to measure it.');
    return;
  }
  badge.textContent = String(score);
  const n = ch.health_score_sample_count || 0;
  const lines = [
    `${healthBandName(score)} - score ${score} / 100`,
    `Lifetime health score over ${n} observation${n === 1 ? '' : 's'}.`,
  ];
  if (ch.last_test_status === 'FAILED') lines.push('⚠ The most recent health check FAILED.');
  else if (ch.last_test_error_detail) lines.push(`⚠ ${ch.last_test_error_detail}`);
  badge.dataset.tip = escTipAttr(lines.join('\n'));
}


// Resolution / frame rate / bitrate / audio, each user-toggleable (12.3), separated by a
// middot at --text-muted - the separator is punctuation between two readable values, and
// --border is a hairline colour too faint to use for a glyph (12.4). A channel with no
// successful health check reads `no stream data` rather than showing blanks.
function renderTechReadout(ch) {
  const box = document.querySelector(`.guide-channel-tech[data-channel-id="${ch.id}"]`);
  if (!box) return;
  if (!techFieldsEnabled()) { box.innerHTML = ''; return; }

  const hasData = ch.last_test_status === 'COMPLETED' &&
    (ch.last_test_resolution || ch.last_test_fps || ch.last_test_bitrate_kbps || ch.last_test_audio_codec);
  if (!hasData) {
    box.innerHTML = '<span class="guide-tech tip-plain" data-tip="' + escTipAttr(
      'No stream details\nThese are measured by a health check. This channel has not been tested successfully yet.') +
      '">no stream data</span>';
    return;
  }

  const bits = [];
  if (LAYOUT.ch_resolution && ch.last_test_resolution) {
    const height = ch.last_test_resolution.split('x')[1];
    bits.push([height ? `${height}p` : ch.last_test_resolution,
      `Video resolution\n${ch.last_test_resolution}, measured on the last successful health check.`]);
  }
  if (LAYOUT.ch_fps && ch.last_test_fps) {
    bits.push([`${Math.round(ch.last_test_fps)}fps`,
      `Frame rate\n${ch.last_test_fps.toFixed(2)} frames per second as reported by the stream.`]);
  }
  if (LAYOUT.ch_bitrate && ch.last_test_bitrate_kbps) {
    const mbps = ch.last_test_bitrate_kbps / 1000;
    bits.push([`${mbps.toFixed(1)} Mbps`,
      `Video bitrate\nAverage ${mbps.toFixed(1)} Mbps, so roughly ${fmtBytes(ch.last_test_bitrate_kbps * 1000 / 8 * 3600)} per hour recorded.`]);
  }
  if (LAYOUT.ch_audio && ch.last_test_audio_codec) {
    const chans = ch.last_test_audio_channels;
    const label = chans ? `${ch.last_test_audio_codec} ${chans}ch` : ch.last_test_audio_codec;
    bits.push([label,
      `Audio track\n${label} - codec and channel count of the stream's primary audio track.`]);
  }
  if (!bits.length) { box.innerHTML = ''; return; }

  box.innerHTML = bits.map(([text, tip], i) =>
    (i ? '<span class="guide-tech-sep">·</span>' : '') +
    `<span class="guide-tech tip-plain" data-tip="${escTipAttr(tip)}">${escHtml(text)}</span>`
  ).join('');
}


// ── Health filter ─────────────────────────────────────────────────────────────

function applyHealthFilter() {
  channelData.forEach(ch => {
    const sidebarRow = document.querySelector(`.guide-channel-row[data-channel-id="${ch.id}"]`);
    const progRow    = document.querySelector(`.guide-row[data-channel-id="${ch.id}"]`);

    [sidebarRow, progRow].forEach(el => {
      if (el) el.classList.remove('channel-health-failed', 'channel-health-warn');
    });
    if (sidebarRow) {
      const old = sidebarRow.querySelector('.health-fail-badge');
      if (old) old.remove();
    }

    if (ch.last_test_status === 'FAILED') {
      const hasActiveRec = (ch.recordings || []).some(r =>
        r.status === 'SCHEDULED' || r.status === 'IN_PROGRESS' || r.status === 'RETRYING');
      const cls = hasActiveRec ? 'channel-health-warn' : 'channel-health-failed';
      [sidebarRow, progRow].forEach(el => {
        if (el) el.classList.add(cls);
      });
      if (hasActiveRec && sidebarRow) {
        const badge = document.createElement('span');
        badge.className = 'health-fail-badge tip-plain';
        badge.dataset.tip = 'Health check FAILED - but this channel has a scheduled recording';
        badge.textContent = '⚠ Health FAIL';
        const info = sidebarRow.querySelector('.guide-channel-info');
        if (info) info.appendChild(badge);
      }
    }
  });
}

// ── Saved searches ──────────────────────────────────────────────────────────
// Shared list backing both the instant filter bar and the extended search modal -
// see /api/guide/saved-searches in app/routes/guide.py.

let savedSearches = [];

function fetchSavedSearches() {
  fetch(GUIDE_CONFIG.savedSearchesUrl)
    .then(r => r.json())
    .then(data => { savedSearches = data.results || []; })
    .catch(() => {});
}

function isQuerySaved(q) {
  const lower = q.trim().toLowerCase();
  return savedSearches.some(s => s.query.toLowerCase() === lower);
}

function saveSearch(query, saveBtn, onSaved) {
  jsonFetch(GUIDE_CONFIG.savedSearchesUrl, {
    method: 'POST',
    body: JSON.stringify({ query }),
  })
    .then(data => {
      if (!savedSearches.some(s => s.id === data.id)) {
        savedSearches.push({ id: data.id, query: data.query });
      }
      if (saveBtn) {
        const input = saveBtn.parentElement && saveBtn.parentElement.querySelector('input');
        if (input) updateSaveButtonVisibility(input, saveBtn);
      }
      if (onSaved) onSaved();
    })
    .catch(() => {});
}

function deleteSavedSearch(id, onDeleted) {
  jsonFetch(GUIDE_CONFIG.deleteSavedSearchUrlBase + id + '/delete', { method: 'POST' })
    .then(() => {
      savedSearches = savedSearches.filter(s => s.id !== id);
      if (onDeleted) onDeleted();
    })
    .catch(() => {});
}

function updateSaveButtonVisibility(inputEl, saveBtn) {
  const val = inputEl.value.trim();
  const saved = isQuerySaved(val);
  // The star is FILLED when the current text is already saved rather than vanishing, so the
  // control's state is legible instead of the affordance simply disappearing (DESIGN.md 13.4,
  // the same on desktop). Visibility itself is CSS's job on a guide toolbar - it keys off
  // .has-value per 3.11 - so no inline display is set there. Both of today's call sites are
  // in one; the fallback is what a search box mounted anywhere else would need.
  saveBtn.classList.toggle('is-saved', saved && val.length >= 2);
  if (!saveBtn.closest('.guide-toolbar')) {
    saveBtn.style.display = (val.length >= 2 && !saved) ? '' : 'none';
  }
}

function renderSavedSearchDropdown(dropdownEl, inputEl, saveBtn, onSelect) {
  updateSaveButtonVisibility(inputEl, saveBtn);

  const filterVal = inputEl.value.trim().toLowerCase();
  const matches = filterVal
    ? savedSearches.filter(s => s.query.toLowerCase().includes(filterVal))
    : savedSearches;

  if (matches.length === 0) {
    dropdownEl.innerHTML = '<div class="saved-search-empty">No saved searches yet</div>';
    return;
  }

  dropdownEl.innerHTML = matches.map(s => `
    <div class="saved-search-row" data-id="${s.id}">
      <span class="saved-search-text">${escHtml(s.query)}</span>
      <button type="button" class="saved-search-delete" data-id="${s.id}"
              title="Delete saved search" aria-label="Delete saved search">&times;</button>
    </div>
  `).join('');

  dropdownEl.querySelectorAll('.saved-search-row').forEach(row => {
    row.addEventListener('click', function (e) {
      if (e.target.closest('.saved-search-delete')) return;
      const match = savedSearches.find(s => s.id === Number(row.dataset.id));
      if (match) onSelect(match.query);
      dropdownEl.style.display = 'none';
    });
  });

  dropdownEl.querySelectorAll('.saved-search-delete').forEach(btn => {
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      deleteSavedSearch(Number(btn.dataset.id), () => {
        renderSavedSearchDropdown(dropdownEl, inputEl, saveBtn, onSelect);
      });
    });
  });
}

function wireSavedSearchUI(inputId, saveBtnId, dropdownId, wrapId, onSelect) {
  const inputEl = document.getElementById(inputId);
  const saveBtn = document.getElementById(saveBtnId);
  const dropdownEl = document.getElementById(dropdownId);
  const wrapEl = document.getElementById(wrapId);
  if (!inputEl || !saveBtn || !dropdownEl || !wrapEl) return;

  inputEl.addEventListener('focus', function () {
    renderSavedSearchDropdown(dropdownEl, inputEl, saveBtn, onSelect);
    dropdownEl.style.display = '';
  });

  inputEl.addEventListener('input', function () {
    updateSaveButtonVisibility(inputEl, saveBtn);
    if (dropdownEl.style.display !== 'none') {
      renderSavedSearchDropdown(dropdownEl, inputEl, saveBtn, onSelect);
    }
  });

  saveBtn.addEventListener('click', function () {
    const q = inputEl.value.trim();
    if (q.length >= 2) {
      saveSearch(q, saveBtn, () => renderSavedSearchDropdown(dropdownEl, inputEl, saveBtn, onSelect));
    }
  });

  document.addEventListener('click', function (e) {
    if (!wrapEl.contains(e.target)) dropdownEl.style.display = 'none';
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────────

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', function () {
  // Grid-only wiring - absent on pages that embed only the search/record modals
  // (recording detail), which have no guide grid, nav bar, or instant-search box.
  const hasGrid = !!document.getElementById('guide-program-area');
  let searchTimer = null;
  if (hasGrid) {
    // Navigation - scroll instead of refetch. The .timenav segmented control carries its jump
    // size in data-nav, so one delegated listener covers all four jumps and adding a jump size
    // is markup-only. Every id below belongs to the channel detail page's own control bar,
    // which has no .timenav - all null-guarded, since neither page has both.
    const timenav = document.getElementById('guide-timenav');
    if (timenav) {
      timenav.addEventListener('click', e => {
        const btn = e.target.closest('button[data-nav]');
        if (btn) scrollGuide(Number(btn.dataset.nav));
      });
    }
    const btnNow = document.getElementById('btn-now');
    if (btnNow) btnNow.addEventListener('click', scrollToNow);

    wireDayPicker();
    wireChannelTaps();

    // Crossing the breakpoint changes the time scale, which Layout applies and the whole
    // sticky stack, so the grid is rebuilt rather than left half in the other mode.
    MOBILE_MQ.addEventListener('change', () => {
      closeSheet();
      applyBreakpoint();
      renderGuide();
      scrollToNow();
    });
    window.addEventListener('resize', () => {
      measureTopBarHeight();
      measureToolbarHeight();
    });

    // The header track lives outside the scroller on the guide, so it is repositioned on
    // every scroll frame - not debounced, or it visibly lags the grid. Only the readout is.
    document.getElementById('guide-program-area').addEventListener('scroll', () => {
      syncHeadTrack();
      if (labelDebounce) clearTimeout(labelDebounce);
      labelDebounce = setTimeout(() => { updateReadout(); }, 60);
    });

    // Instant search (debounced)
    const guideSearchWrap = document.querySelector('.guide-search-wrap');
    document.getElementById('guide-search').addEventListener('input', function () {
      if (guideSearchWrap) guideSearchWrap.classList.toggle('has-value', this.value.length > 0);
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => renderGuide(), 200);
    });

    const guideSearchClear = document.getElementById('guide-search-clear');
    if (guideSearchClear) {
      guideSearchClear.addEventListener('click', () => {
        const input = document.getElementById('guide-search');
        input.value = '';
        if (guideSearchWrap) guideSearchWrap.classList.remove('has-value');
        clearTimeout(searchTimer);
        renderGuide();
        input.focus();
      });
    }
  }

  fetchSavedSearches();
  if (hasGrid) {
    wireSavedSearchUI('guide-search', 'guide-search-save', 'guide-search-dropdown', 'guide-search-wrap', function (query) {
      const input = document.getElementById('guide-search');
      input.value = query;
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
  }

  // Tag filter dropdown - absent when no tags are configured
  const tagFilterWrap = document.getElementById('tag-filter-wrap');
  if (tagFilterWrap) {
    const tagFilterToggle = document.getElementById('tag-filter-toggle');
    const tagFilterPanel = document.getElementById('tag-filter-panel');
    tagFilterToggle.addEventListener('click', function (e) {
      e.stopPropagation();
      // 13.10's Tags sheet drives these same inputs, so the panel stays in the DOM at phone
      // widths (hidden by CSS) and remains the single source of filter state.
      if (isMobileGuide()) { tagFilterPanel.style.display = 'none'; openTagsSheet(); return; }
      tagFilterPanel.style.display = tagFilterPanel.style.display === 'none' ? '' : 'none';
      syncScrollLock();
    });
    document.addEventListener('click', function (e) {
      if (!tagFilterWrap.contains(e.target)) {
        tagFilterPanel.style.display = 'none';
        syncScrollLock();
      }
    });
    tagFilterPanel.querySelectorAll('input').forEach(input => {
      input.addEventListener('change', function () {
        updateTagFilterSummary();
        renderGuide();
      });
    });
  }

  // Duration filter dropdown - unlike Tags, always present: program length has no
  // configuration dependency the way tags do.
  const durFilterWrap = document.getElementById('dur-filter-wrap');
  if (durFilterWrap) {
    const durFilterToggle = document.getElementById('dur-filter-toggle');
    const durFilterPanel = document.getElementById('dur-filter-panel');
    durFilterToggle.addEventListener('click', function (e) {
      e.stopPropagation();
      // Same reasoning as the Tags sheet: the panel's own inputs stay the single source of
      // filter state, mobile just drives them through a sheet instead of the dropdown.
      if (isMobileGuide()) { durFilterPanel.style.display = 'none'; openLengthSheet(); return; }
      durFilterPanel.style.display = durFilterPanel.style.display === 'none' ? '' : 'none';
      syncScrollLock();
    });
    document.addEventListener('click', function (e) {
      if (!durFilterWrap.contains(e.target)) {
        durFilterPanel.style.display = 'none';
        syncScrollLock();
      }
    });
    // Debounced like the search box: these are typed number inputs, not discrete clicks.
    let durFilterTimer = null;
    const applyDurFilterChange = () => {
      clearTimeout(durFilterTimer);
      durFilterTimer = setTimeout(() => { updateDurFilterSummary(); renderGuide(); }, 200);
    };
    durFilterPanel.querySelectorAll('input[type="number"]').forEach(input => {
      input.addEventListener('input', applyDurFilterChange);
    });
    const durFilterUnit = document.getElementById('dur-filter-unit');
    if (durFilterUnit) {
      durFilterUnit.addEventListener('change', () => { updateDurFilterSummary(); renderGuide(); });
    }
    const durFilterClear = document.getElementById('dur-filter-clear');
    if (durFilterClear) {
      durFilterClear.addEventListener('click', function (e) {
        e.stopPropagation();
        clearTimeout(durFilterTimer);
        clearDurationFilter();
      });
    }
  }

  /* "Search all..." - the TV Guide's way into the AIRINGS search on /channels. It
     replaced the Extended Search modal (dev/changelog/416), which was this app's second
     airing search. A button rather than a link because it carries the filter box's LIVE
     text: the base URL is rendered by the server through SearchState.to_params(), and `q`
     is the only parameter spelled here. */
  const btnAiringSearch = document.getElementById('btn-airing-search');
  if (btnAiringSearch) {
    btnAiringSearch.addEventListener('click', () => {
      const base = btnAiringSearch.dataset.airingSearchUrl;
      if (!base) return;
      const box = document.getElementById('guide-search');
      const q = box ? box.value.trim() : '';
      location.href = q
        ? `${base}${base.includes('?') ? '&' : '?'}q=${encodeURIComponent(q)}`
        : base;
    });
  }

  // Record modal close
  document.getElementById('modal-cancel').addEventListener('click',  closeModal);
  document.getElementById('modal-close').addEventListener('click',   closeModal);
  document.getElementById('modal-backdrop').addEventListener('click', closeModal);

  // Recompute padded start/stop when the profile selection changes (new recordings only) -
  // but never for a field the user has since hand-edited (dev/docs/BUGS.md 2026-08-06).
  const modalStartInput = document.getElementById('modal-start');
  const modalStopInput  = document.getElementById('modal-stop');
  if (modalStartInput) modalStartInput.addEventListener('input', () => { _modalStartEdited = true; });
  if (modalStopInput)  modalStopInput.addEventListener('input',  () => { _modalStopEdited = true; });
  const modalProfileSel = document.getElementById('modal-profile');
  if (modalProfileSel) modalProfileSel.addEventListener('change', applyProfilePadding);

  // Active recording modal
  document.getElementById('active-rec-close').addEventListener('click', closeActiveRecModal);
  document.getElementById('active-rec-close-x').addEventListener('click', closeActiveRecModal);
  document.getElementById('active-rec-backdrop').addEventListener('click', closeActiveRecModal);

  document.getElementById('active-rec-save-stop').addEventListener('click', async function () {
    const recId = _activeRecId;
    if (!recId) return;
    const stopVal = document.getElementById('active-rec-stop').value;
    if (!stopVal) { showActiveRecError('Please enter a stop time.'); return; }
    document.getElementById('active-rec-error').style.display = 'none';
    try {
      await jsonFetch('/recordings/' + recId + '/adjust-stop', {
        method: 'POST',
        body: JSON.stringify({ stop_time: stopVal }),
      });
      closeActiveRecModal();
      fetchAndRender();
    } catch (e) {
      showActiveRecError(e.message || 'Could not update stop time.');
    }
  });

  document.getElementById('active-rec-stop-btn').addEventListener('click', function () {
    activeRecAction('/stop-json', 'Stop the recording now and concatenate what was captured?');
  });

  document.getElementById('active-rec-pause-btn').addEventListener('click', function () {
    const isPaused = this.dataset.paused === '1';
    if (isPaused) {
      activeRecAction('/resume-json', null);
    } else {
      activeRecAction('/pause-json', null);
    }
  });

  document.getElementById('active-rec-cancel-btn').addEventListener('click', function () {
    activeRecAction('/cancel-json', 'Abort this recording? Capture will stop and every recorded segment will be permanently deleted from disk. This cannot be undone.');
  });

  // Stream URL mask/reveal + copy
  document.getElementById('modal-url-toggle').addEventListener('click', function () {
    const display = document.getElementById('modal-url-display');
    const raw = document.getElementById('modal-url').value;
    const revealed = display.dataset.revealed === 'true';
    display.value = revealed ? maskCreds(raw) : raw;
    display.dataset.revealed = revealed ? 'false' : 'true';
    this.textContent = revealed ? 'Show' : 'Hide';
  });

  document.getElementById('modal-url-copy').addEventListener('click', function () {
    const raw = document.getElementById('modal-url').value;
    const btn = this;
    const done = () => { btn.textContent = 'Copied!'; setTimeout(() => { btn.textContent = 'Copy'; }, 2000); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(raw).then(done).catch(() => fallbackCopy(raw, done));
    } else {
      fallbackCopy(raw, done);
    }
  });

  // Modal form - intercept submit and stay on guide. Replace mode instead navigates to the
  // new recording's detail page (the page's own recording was just deleted server-side).
  document.getElementById('modal-form').addEventListener('submit', async function(e) {
    e.preventDefault();
    document.getElementById('modal-error').style.display = 'none';
    const data = new URLSearchParams(new FormData(this));
    await submitModalForm(this, data);
  });

  // Cancel (scheduled-recording) button
  document.getElementById('modal-delete').addEventListener('click', async function () {
    const recId = this.dataset.recId;
    if (!recId) return;
    if (!confirm('Cancel scheduled recording? This scheduled recording will be removed.')) return;
    document.getElementById('modal-error').style.display = 'none';
    try {
      await jsonFetch(GUIDE_CONFIG.editRecordingUrlBase + recId + '/cancel-json', { method: 'POST' });
      closeModal();
      fetchAndRender();
      if (typeof window.onScheduleSaved === 'function') window.onScheduleSaved();
    } catch (e) {
      showModalError(e.message || 'Cancel failed.');
    }
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      closeModal();
      closeActiveRecModal();
    }
  });

  // The channel column's click handling lives in wireChannelTaps() - one listener covering
  // both the desktop navigation and the mobile channel sheet.

  // "Include failed channels" now lives in the Layout popover (12.3) and persists with the
  // rest of it. This applies the server-rendered initial state to the grid so the markup the
  // server sent already equals the state the user last chose - JS upgrades it, it is never
  // required to calm the page down afterwards.
  applyShowFailed();
  wireLayoutPopover();
  wireSortControl();

  // Initial load + periodic refresh - grid pages only
  if (hasGrid) {
    // Before initWindow(), because the time scale it establishes is breakpoint-dependent.
    applyBreakpoint();
    initWindow();
    fetchAndRender();

    // Refresh now-line every minute
    setInterval(updateNowLine, 60000);

    // Auto-refresh EPG every 5 minutes (preserves scroll position via fetchAndRender)
    setInterval(fetchAndRender, 5 * 60 * 1000);
  }

  // Re-render program cells on viewport width change (rotation, browser resize).
  // Intentionally ignores height-only changes so that showing/hiding the mobile
  // keyboard (which only changes viewport height) does not wipe the active filter.
  let resizeTimer = null;
  let lastViewportWidth = window.innerWidth;
  window.addEventListener('resize', function () {
    const newWidth = window.innerWidth;
    if (newWidth === lastViewportWidth) return;
    lastViewportWidth = newWidth;
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      // The toolbar wraps at narrow widths, which moves the grid header's sticky offset -
      // re-measure before re-rendering rather than letting the stack desync.
      measureToolbarHeight();
      if (channelData.length) renderGuide();
    }, 150);
  });
});
