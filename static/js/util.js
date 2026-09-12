// Shared frontend helpers - canonical home per CLAUDE.md's coding-standards table.
// Loaded from base.html <head> so every page (including inline content-block scripts)
// can rely on these. Plain function declarations only: a leftover page-local duplicate
// `function` re-declaration is tolerated by the engine, a `const` collision would throw.

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Glob-style wildcards (`*` = any run, `?` = one character) for client-side filtering -
// the JS counterpart to app/search_index.py's has_wildcards()/glob_to_like(), which do the
// same job server-side for SQL LIKE. Deliberately not regex: only * and ? are special,
// everything else in the term is escaped literally. A term with no wildcard character
// still matches (unanchored substring, same as always) via globRegExpBody's plain-escape
// branch - this is the switch between the two, not a mode the user selects.
function hasWildcard(term) {
  return /[*?]/.test(term);
}

// The regex source (no flags) for a glob term, unanchored so it behaves as a substring
// match. Exposed separately from globMatch so callers building several terms into one
// highlight pass (channel-search.js's highlight()) can compose them.
function globRegExpBody(term) {
  return hasWildcard(term)
    ? term.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.')
    : term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function globMatch(text, term) {
  if (!term) return true;
  return new RegExp(globRegExpBody(term), 'i').test(String(text ?? ''));
}

// null/undefined → '-' (unknown), 0 → '0 B'; scales to TB with one decimal.
function fmtBytes(n) {
  if (n === null || n === undefined) return '-';
  if (n < 1024) return `${Math.round(n)} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(1)} ${units[i]}`;
}

function nf(n) {
  return Number(n || 0).toLocaleString();
}

// withSeconds=true (default) is for live tickers (elapsed/remaining/downtime) - every
// current call site is one of those, so seconds show at every scale, not just under 1h.
// withSeconds=false is the bare DESIGN.md 9.1 form ('3h 1m' / '2h' / '42m') for
// static/summary durations, which are otherwise server-rendered via the `duration`
// Jinja filter (app/routes/recordings.py::_fmt_duration) - keep the two in sync.
function fmtDur(s, withSeconds = true) {
  s = Math.floor(Math.max(0, s || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (withSeconds) {
    if (h) return `${h}h ${m}m ${sec}s`;
    if (m) return `${m}m ${sec}s`;
    return `${sec}s`;
  }
  if (h && m) return `${h}h ${m}m`;
  if (h) return `${h}h`;
  return `${m}m`;
}

// ── The time-axis scale (DESIGN.md 13.1, 16.3) ───────────────────────────────
// Desktop is 4px/min (240px/hr); mobile is 3px/min (30 min = 90px). These are MEASURED
// densities, not guesses - 13.1 records how they were arrived at.
//
// They live here rather than in guide.js because there are now two surfaces drawing a
// horizontal time axis at the same scale: the TV Guide grid and the Live Dashboard's
// timeline. DESIGN.md 16.3 is explicit that the dashboard READS the scale rather than
// copying it - "a second hand-typed copy diverges the first time either is tuned, and the
// divergence is invisible on either page alone." One definition, two readers.
//
// Every reader takes the value at call time: the scale is a property of the breakpoint, and
// the breakpoint can change under a live page (rotation, a resized window).
const PX_PER_MIN_DESKTOP = 4;
const PX_PER_MIN_MOBILE = 3;

// Where a time axis parks NOW on first paint: a third of the way across the viewport, so
// enough of the recent past is visible to see what just finished while most of the width is
// spent on what has not happened yet. Shared for the same reason as the scale above - the
// two axes must open the same way.
const NOW_SCROLL_DIVISOR = 3;

// Date instant -> `datetime-local` input value ('YYYY-MM-DDTHH:MM') showing the wall-clock
// time in `tz`, never the browser's timezone.
//
// guide.js used to carry its own utcToLocalInput/tzMidnightUTC doing the identical
// offset-correction trick behind a one-line `new Date(iso + 'Z')` adapter (and a
// zero-padded 'T00:00' string adapter for tzInputValueToDate). Those were removed and its
// six call sites now call these two functions directly - see dev/changelog/622.
function dateToTzInputValue(date, tz) {
  const p = {};
  new Intl.DateTimeFormat('en-US', {
    timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).formatToParts(date).forEach(({ type, value }) => { p[type] = value; });
  const h = p.hour === '24' ? '00' : p.hour;
  return `${p.year}-${p.month}-${p.day}T${h}:${p.minute}`;
}

// Reverse of dateToTzInputValue: a `datetime-local` value ('YYYY-MM-DDTHH:MM') meant as a
// wall-clock time in `tz` -> the real Date instant it refers to.
function tzInputValueToDate(value, tz) {
  const [datePart, timePart] = value.split('T');
  const [y, mo, d] = datePart.split('-').map(Number);
  const [h, mi] = timePart.split(':').map(Number);
  const guess = new Date(Date.UTC(y, mo - 1, d, h, mi, 0));
  const p = {};
  new Intl.DateTimeFormat('en-US', {
    timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).formatToParts(guess).forEach(({ type, value }) => { p[type] = value; });
  const hh = p.hour === '24' ? 0 : parseInt(p.hour, 10);
  const asIfLocal = Date.UTC(+p.year, +p.month - 1, +p.day, hh, +p.minute, +p.second);
  return new Date(guess.getTime() - (asIfLocal - guess.getTime()));
}

// ── Display timezone and clock format ────────────────────────────────────────
// The client half of time rendering. Server-rendered content formats through
// app/tz_utils.py and never touches any of this; these cover what JavaScript draws
// itself - SSE timestamps, live clocks, the guide's time ruler, client-built tooltips.
//
// Both settings come from the <meta> tags base.html puts on every page, so a page needs
// no config plumbing of its own to render a time correctly. Eleven templates used to
// inject the same two values into their own JS globals in four different spellings
// (`tz:` vs `timezone:`, two opposite Jinja conditionals for the boolean, one inverted
// `format24`), and the page that never got the plumbing at all - the dashboard timeline -
// silently rendered the BROWSER's timezone instead of the configured one for as long as
// it existed. dev/changelog/654, dev/docs/BUGS.md 2026-08-14.
//
// Memoized on first read: the tags are static for the life of the document.
let _displayTz = null;
function displayTz() {
  if (_displayTz === null) {
    const meta = document.querySelector('meta[name="display-tz"]');
    _displayTz = (meta && meta.content) || 'UTC';
  }
  return _displayTz;
}

let _displayHour12 = null;
function displayHour12() {
  if (_displayHour12 === null) {
    const meta = document.querySelector('meta[name="display-hour12"]');
    _displayHour12 = !meta || meta.content !== 'false';
  }
  return _displayHour12;
}

// ---------------------------------------------------------------------------
// Health-score bands
// ---------------------------------------------------------------------------
// The browser's half of app/health_bands.py. base.html serves the resolved bands in a meta
// tag, so the numbers live in exactly one place across Python, Jinja and JS - before
// dev/changelog/771 the `< 50 / < 80` ternary was re-typed at eleven sites and could not be
// changed together. Memoized like displayTz(): the tag is static for the life of the page.
let _healthBands = null;
function healthBands() {
  if (_healthBands === null) {
    const meta = document.querySelector('meta[name="health-bands"]');
    let parsed = null;
    try {
      parsed = meta && meta.content ? JSON.parse(meta.content) : null;
    } catch (e) {
      // A malformed tag must not take the whole page's JS down with it - band everything as
      // untested and say so, which is the honest reading of "we do not know the cut points".
      console.error('health-bands meta is not valid JSON', e);
    }
    _healthBands = (parsed && Array.isArray(parsed.bands)) ? parsed : { bands: [], untested: { key: 'untested', label: 'Never tested', css: 'hb-none' } };
  }
  return _healthBands;
}

// The band key for a score - 'great' | 'good' | 'fair' | 'poor' | 'untested'.
function healthBand(score) {
  if (score === null || score === undefined) return 'untested';
  const bands = healthBands().bands;
  for (const b of bands) {
    if (score >= b.floor) return b.key;
  }
  // Below every floor: only reachable for a negative score (manual adjustment is applied
  // unclamped), which the bottom band is the nearest answer for.
  return bands.length ? bands[bands.length - 1].key : 'untested';
}

// The `.hb-*` CSS modifier for a score, and the band's label with its configured range.
function healthBandCss(score) {
  return `hb-${healthBand(score)}`;
}

function healthBandLabel(score) {
  return _bandField(score, 'label');
}

// The band's bare name ("Fair"), for prose that already shows the number beside it.
function healthBandName(score) {
  return _bandField(score, 'name');
}

function _bandField(score, field) {
  const key = healthBand(score);
  if (key === 'untested') return healthBands().untested[field] || '';
  const band = healthBands().bands.find(b => b.key === key);
  return band ? band[field] : '';
}

// Cached Intl.DateTimeFormat for `opts` in the display timezone.
//
// The cache is load-bearing, not an optimization: channel-search.js formats a time and a
// day label for EVERY row of a result page, and a formatter constructed per call would put
// Intl construction inside a per-row loop - the JS side of CLAUDE.md's no-hidden-I/O rule,
// on a page that searches 136,130 channels. Every hand-rolled site this replaced hoisted
// its own formatter to module scope for that reason; the shared helper has to keep that
// property or converting them would be a regression.
//
// hour12 is filled in from the setting whenever the caller asks for an hour and did not
// pin it explicitly, so a combined date+time format cannot silently ignore the clock
// setting the way an ad-hoc option object could.
//
// Locale is en-US everywhere. The app's own strings are English by construction (the
// literal 'Today'/'Tomorrow' below, check-modal.js's CC_WEEKDAYS/CC_MONTHS), so a
// browser-locale month name renders half-translated output next to them - which is what
// channel-search.js alone used to do.
const _tzFormatters = new Map();
function tzFormatter(opts, locale = 'en-US') {
  const full = Object.assign({ timeZone: displayTz() }, opts);
  if (full.hour !== undefined && full.hour12 === undefined) full.hour12 = displayHour12();
  const key = `${locale}|${JSON.stringify(full)}`;
  let f = _tzFormatters.get(key);
  if (!f) {
    f = new Intl.DateTimeFormat(locale, full);
    _tzFormatters.set(key, f);
  }
  return f;
}

// Clock time in the display timezone: '9:05 PM', or '21:05:33' with seconds under a 24h
// setting. `seconds` is for live tickers and event logs, where the second is the point.
function fmtTimeTz(date, { seconds = false } = {}) {
  const opts = { hour: 'numeric', minute: '2-digit' };
  if (seconds) opts.second = '2-digit';
  return tzFormatter(opts).format(date);
}

// Date parts in the display timezone. Defaults to the long day form the guide's readout
// and day picker use; pass Intl options for any other shape.
function fmtDateTz(date, opts) {
  return tzFormatter(opts || { weekday: 'long', month: 'short', day: 'numeric' }).format(date);
}

// 'YYYY-MM-DD' for the instant's calendar day in the display timezone. en-CA is
// ISO-ordered, which makes the key sortable and comparable as a plain string - which is
// the whole reason day bucketing uses a formatted key rather than date arithmetic.
function tzDayKey(date) {
  return tzFormatter({ year: 'numeric', month: '2-digit', day: '2-digit' }, 'en-CA').format(date);
}

// 'Today' / 'Tomorrow' / 'Yesterday', else the date. Naming the day in words is what stops
// a column of times reading as one run of numbers.
//
// The comparison clock is read fresh on every call, never captured: these pages are
// long-lived, and a row that said 'Today' at 11pm must not still say it at 1am.
function tzDayLabel(date, opts) {
  if (!date) return '';
  const now = new Date();
  const key = tzDayKey(date);
  if (key === tzDayKey(now)) return 'Today';
  if (key === tzDayKey(new Date(now.getTime() + 86400000))) return 'Tomorrow';
  if (key === tzDayKey(new Date(now.getTime() - 86400000))) return 'Yesterday';
  return fmtDateTz(date, opts || { weekday: 'short', month: 'short', day: 'numeric' });
}

// A naive-UTC timestamp from the API -> the real instant. The app stores naive UTC
// everywhere and JavaScript reads an unsuffixed string as browser-LOCAL, so the `Z` has to
// go back on - the single most-repeated line in the code this replaced. Tolerates a string
// that already carries an offset, and returns null rather than an Invalid Date.
function utcIsoToDate(iso) {
  if (!iso) return null;
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
  return isNaN(d.getTime()) ? null : d;
}

// True when a live, non-empty text selection intersects `el`. Live-updating log views call
// this to suppress auto-scroll and row trimming: appending nodes leaves a selection intact,
// but scrolling the container yanks the highlighted line off screen and trimming a node the
// selection covers collapses it outright. Scoped to `el` so a selection elsewhere on the page
// can never freeze the tail.
function hasSelectionIn(el) {
  const sel = window.getSelection();
  if (!el || !sel || !sel.rangeCount || sel.isCollapsed) return false;
  for (let i = 0; i < sel.rangeCount; i++) {
    if (el.contains(sel.getRangeAt(i).commonAncestorContainer)) return true;
  }
  return false;
}

let _toastTimer = null;
function showToast(message, { type = 'success', html = false, durationMs = 5000 } = {}) {
  let t = document.getElementById('app-toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'app-toast';
    document.body.appendChild(t);
  }
  if (html) t.innerHTML = message; else t.textContent = message;
  t.className = `toast toast-${type} toast-show`;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { t.className = 'toast'; }, durationMs);
}

// Render a message + clickable conflict-recording list into a modal error element.
function showConflictError(elId, msg, conflicts, recDetailUrlBase) {
  const el = document.getElementById(elId);
  el.textContent = '';
  el.appendChild(document.createTextNode(msg));
  if (conflicts && conflicts.length) {
    const ul = document.createElement('ul');
    ul.style.margin = '0.4rem 0 0 1.1rem';
    ul.style.padding = '0';
    conflicts.forEach(c => {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = recDetailUrlBase + c.recording_id;
      a.target = '_blank';
      a.rel = 'noopener';
      a.style.color = 'inherit';
      a.textContent = `${c.title} (${c.channel_name}) - ${c.start_time} to ${c.stop_time}`;
      li.appendChild(a);
      ul.appendChild(li);
    });
    el.appendChild(ul);
  }
  el.style.display = 'block';
}

// The soft, proceedable warnings POST /recordings/new-json and .../edit-json answer with
// when a schedule overlaps another recording ({success: false, overlap_warning,
// connection_limit_warning}) - dev/changelog/858, the recording-scheduling counterpart to
// group-modal.js's groupWarningsHtml(). Reuses the same
// {recording_id, channel_name, title, start_time, stop_time} conflict shape showConflictError
// already knows how to draw, so both blocks render as prose plus a clickable list.
//
// Returns the warning markup only, no button - the caller owns the "Proceed anyway" click
// (what "proceed" resubmits is the caller's own request), same division of labor as
// groupWarningsHtml's forceId.
function recordingWarningsHtml(data, recDetailUrlBase) {
  const block = (heading, w) => {
    if (!w) return '';
    const items = (w.conflicts || []).map((c) =>
      `<li><a href="${recDetailUrlBase}${c.recording_id}" target="_blank" rel="noopener" style="color:inherit;">`
      + `${escHtml(c.title)} (${escHtml(c.channel_name)}) - ${escHtml(c.start_time)} to ${escHtml(c.stop_time)}</a></li>`
    ).join('');
    return `<div style="margin-bottom:0.5rem;">
      <strong style="color:var(--warn);">⚠ ${escHtml(heading)}</strong><br>
      <span class="text-muted small">${escHtml(w.message)}</span>
      ${items ? `<ul class="small" style="margin:0.35rem 0 0; padding-left:1.25rem;">${items}</ul>` : ''}
    </div>`;
  };
  return block('Overlapping recording', data.overlap_warning)
    + block('Connection limit', data.connection_limit_warning);
}

// EventSource with auto-reconnect. Returns { close() }; close() stops reconnection.
function connectSSE(url, { onMessage, onOpen = null, onError = null, reconnectMs = 5000 } = {}) {
  let src = null;
  let closed = false;
  function open() {
    src = new EventSource(url);
    if (onOpen) src.onopen = onOpen;
    src.onmessage = onMessage;
    src.onerror = () => {
      if (onError) onError();
      src.close();
      if (!closed && reconnectMs > 0) {
        setTimeout(() => { if (!closed) open(); }, reconnectMs);
      }
    };
  }
  open();
  return {
    close() {
      closed = true;
      if (src) src.close();
    },
  };
}

function csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.content : '';
}

// fetch wrapper: JSON Content-Type when a JSON body is sent (FormData/URLSearchParams
// bodies keep their browser-set Content-Type), X-CSRFToken on state-changing methods,
// parses the JSON response, throws Error(data.error || statusText) on a non-2xx status.
// `opts` reaches fetch() untouched apart from those headers, so `signal` works here and
// an abortable call has no reason to drop to a raw fetch() (dev/changelog/417). An
// aborted call rejects with a DOMException named 'AbortError', never an Error carrying
// data.error - callers that toast failures must let that one through.
async function jsonFetch(url, opts = {}) {
  const isFormBody = opts.body instanceof FormData || opts.body instanceof URLSearchParams;
  if (opts.body !== undefined && opts.body !== null && !isFormBody) {
    opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  }
  const method = (opts.method || 'GET').toUpperCase();
  if (method !== 'GET' && method !== 'HEAD') {
    opts.headers = Object.assign({ 'X-CSRFToken': csrfToken() }, opts.headers || {});
  }
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON body */ }
  if (!res.ok) {
    const err = new Error((data && data.error) || res.statusText);
    err.status = res.status;   // callers needing the raw body (e.g. conflict lists) read err.data
    err.data = data;
    throw err;
  }
  return data;
}

// Global fetch wrap: every same-origin state-changing request gets the CSRF header,
// so raw fetch() call sites (and any future ones) are protected without each one
// remembering the token. Header injection only - bodies (JSON/FormData/URLSearchParams)
// pass through untouched. The token is never sent cross-origin.
(() => {
  const rawFetch = window.fetch;
  window.fetch = (input, init = {}) => {
    const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      if (new URL(url, location.href).origin === location.origin) {
        const headers = new Headers((init && init.headers) || (typeof input !== 'string' && input && input.headers) || undefined);
        if (!headers.has('X-CSRFToken')) headers.set('X-CSRFToken', csrfToken());
        init = Object.assign({}, init, { headers });
      }
    }
    return rawFetch(input, init).then((res) => {
      // The session-expired/gate-denied redirect, in the one place every request
      // passes through - jsonFetch, base.html's raw nav-status poll, and any future
      // call site alike, so a new fetch call never has to remember to handle this
      // itself. Keyed on the header, never the bare status, so it can never fire on
      // a 401 an endpoint returns for its own unrelated reasons.
      if (res.status === 401 && res.headers.get('X-Auth-Required') === '1') {
        window.location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
      }
      return res;
    });
  };
})();

// Re-fetch a page and replace each selected region with its fresh server-rendered copy, so
// a page that keeps itself current has one renderer - the template - rather than a second
// one in JS that can drift from it. A region missing from either side is left alone.
// Listeners bound to anything inside a swapped region go with it (bind by delegation), and
// inline state held on those nodes has to be re-applied once this resolves. Rejects on a
// network error or a non-2xx answer, leaving the page as it was.
async function swapFromServer(selectors, url = location.href) {
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${url} answered ${res.status}`);
  const doc = new DOMParser().parseFromString(await res.text(), 'text/html');
  selectors.forEach((sel) => {
    const current = document.querySelector(sel);
    const fresh = doc.querySelector(sel);
    if (current && fresh) current.outerHTML = fresh.outerHTML;
  });
}

/* ── App design-system behaviors (DESIGN.md sections 3.6/3.7/3.9/3.12) ──
   One delegated handler set per behavior, initialized once at load; pages only
   need the markup conventions (.tip[data-tip], [data-menu] + .menu, .thumb). */

// Portal/position-aware tooltip: one floating .tip-pop element, positioned in
// fixed coordinates so it can never be clipped by scroll containers; prefers
// opening above the trigger and flips below near the top of the viewport.
(() => {
  let pop = null;
  let anchor = null;
  const show = (el) => {
    // Authored tips write their line break as "&#10;". Markup from a template or a page
    // script is parsed as HTML, so that entity is already a real newline by the time it is
    // read back here - but a tip passed as a Jinja MACRO ARGUMENT is autoescaped to
    // "&amp;#10;", so the attribute genuinely holds those six characters, and textContent
    // renders exactly what it is handed (dev/docs/BUGS.md 2026-09-11). Decoded in this one
    // renderer rather than at 126 call sites; both spellings mean the same newline.
    const text = (el.getAttribute('data-tip') || '').replace(/&#10;/g, '\n');
    if (!text) return;
    if (!pop) {
      pop = document.createElement('div');
      pop.className = 'tip-pop';
      document.body.appendChild(pop);
    }
    pop.textContent = text;
    pop.style.display = 'block';
    anchor = el;
    const r = el.getBoundingClientRect();
    const pw = pop.offsetWidth, ph = pop.offsetHeight;
    const minTop = minVisibleTop();
    let left = Math.min(Math.max(8, r.left + r.width / 2 - pw / 2), window.innerWidth - pw - 8);
    let top = r.top - ph - 8;
    if (top < minTop) top = r.bottom + 8;                   // flip below the trigger (or the sticky top bar)
    if (top + ph > window.innerHeight - 8) top = Math.max(minTop, r.top - ph - 8);
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
  };
  const hide = () => {
    if (pop) pop.style.display = 'none';
    anchor = null;
  };
  document.addEventListener('mouseover', (e) => {
    const el = e.target.closest('[data-tip]');
    if (el && el !== anchor) show(el);
    else if (!el && anchor) hide();
  });
  document.addEventListener('scroll', hide, true);
})();

// Sticky-header-aware top clamp (DESIGN.md 9.6/10.4): a tooltip must never render
// under the fixed mobile top bar. Shared by the tooltip and menu positioners below.
function minVisibleTop() {
  const topnav = document.querySelector('.topnav');
  if (topnav && getComputedStyle(topnav).display !== 'none') {
    return topnav.getBoundingClientRect().bottom + 8;
  }
  return 8;
}

// Kebab / popover menus: a [data-menu] button toggles the .menu inside its
// nearest positioned wrapper (.menu-wrap or .c-actions). One open at a time;
// Esc or an outside click closes; clicks on non-action content inside a menu
// (e.g. the Columns checkboxes) don't dismiss it.
//
// Positioning (DESIGN.md 9.6/10.4 - "menus clamp horizontally and open upward
// when the bottom of the window would clip them"): once open, the menu is
// repositioned to `fixed` viewport coordinates computed from the trigger's own
// rect, same portal principle as the tooltip above - the CSS class rules
// (right:0 / .pop-left / .pop-up) still supply the *preferred* side/direction,
// this only clamps the computed result back into the viewport.
function closeMenus() {
  document.querySelectorAll('.menu.open').forEach(m => {
    m.classList.remove('open');
    m.style.position = m.style.left = m.style.top = m.style.right = m.style.bottom = '';
  });
  // Every close path for a menu - Escape, outside click, the scroll listener below - goes
  // through here, so one sync covers all of them.
  syncScrollLock();
}
function positionMenu(menu, trigger) {
  const margin = 8;
  const r = trigger.getBoundingClientRect();
  const preferLeft = menu.classList.contains('pop-left');
  const preferUp = menu.classList.contains('pop-up');
  menu.style.position = 'fixed';
  // defeat the CSS class's right:0 / .pop-up's bottom:calc(...) - inline 'auto'
  // beats a stylesheet value regardless of value, empty string would not.
  menu.style.right = menu.style.bottom = 'auto';
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let left = preferLeft ? r.left : r.right - mw;
  left = Math.max(margin, Math.min(left, window.innerWidth - mw - margin));
  const minTop = minVisibleTop();
  let top = preferUp ? r.top - mh - 5 : r.bottom + 5;
  if (!preferUp && top + mh > window.innerHeight - margin) top = r.top - mh - 5;   // flip up
  else if (preferUp && top < minTop) top = r.bottom + 5;                            // flip down
  top = Math.max(minTop, Math.min(top, window.innerHeight - mh - margin));
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  // Baseline for the scroll listener below: a fixed-positioned menu doesn't travel
  // with its trigger, so it has to close on a real scroll - but only a real one.
  // Re-baselined on every call, including a reposition of an already-open menu.
  menu._menuTrigger = trigger;
  menu._menuTriggerTop = r.top;
}
(() => {
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-menu]');
    if (btn) {
      e.stopPropagation();
      e.preventDefault();
      const wrap = btn.closest('.menu-wrap, .c-actions') || btn.parentElement;
      const menu = wrap && wrap.querySelector('.menu');
      if (!menu) return;
      const wasOpen = menu.classList.contains('open');
      closeMenus();
      if (!wasOpen) {
        menu.classList.add('open');
        positionMenu(menu, btn);
        syncScrollLock();
      }
      return;
    }
    const insideMenu = e.target.closest('.menu.open');
    if (insideMenu) {
      // a real action (link / .menu-item button) closes; other content stays open
      if (e.target.closest('a, button.menu-item')) closeMenus();
      else e.stopPropagation();
      return;
    }
    closeMenus();
  });
  // Escape closes the innermost thing, and an open menu IS the innermost thing. Without the
  // stopImmediatePropagation below, a menu opened from inside a modal took the modal down
  // with it: buildModal's Escape handler is on `document` too, so stopPropagation cannot
  // separate them - only stopping the remaining listeners on this same node can. This
  // listener is registered when util.js loads, before any modal can add its own, which is
  // what makes it the one that runs first.
  // Found by the browser pass on the filename designer, the first surface to open a menu
  // from inside a modal (dev/docs/BUGS.md 2026-08-03; dev/changelog/441).
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!document.querySelector('.menu.open')) return;
    closeMenus();
    e.stopImmediatePropagation();
  });
  // An open menu is positioned in viewport coordinates from the trigger's rect, so it
  // does not travel with the trigger - scrolling would leave it floating over unrelated
  // content. Close it instead of trying to re-follow. Capture, because the scroll may
  // happen in any container (a .table-scroll, a modal body), and scrolling INSIDE the
  // menu itself is exempt - a long menu is allowed to scroll.
  document.addEventListener('scroll', (e) => {
    if (e.target && e.target.closest && e.target.closest('.menu.open')) return;
    const open = document.querySelector('.menu.open');
    if (!open) return;
    // Close on the trigger having actually MOVED, not on a scroll event merely
    // existing. Opening a menu fires a net-zero scroll pair on `document` about 8ms
    // later (likely one frame of `.menu.open` painted absolute before positionMenu()
    // switches it to fixed), which this must not mistake for the user scrolling away
    // - that misread closed every kebab on a scrollable page the instant it opened
    // (dev/docs/BUGS.md 2026-08-04).
    const trigger = open._menuTrigger;
    if (trigger && Math.abs(trigger.getBoundingClientRect().top - open._menuTriggerTop) < 1) return;
    closeMenus();
  }, { capture: true, passive: true });
})();

/* ── Scroll lock ──────────────────────────────────────────────────────
   An open overlay must block scrolling of the page behind it. Four properties, all
   load-bearing (pattern designed and verified in dev/changelog/347; rolled out here in
   dev/changelog/352):

   1. `position: fixed`, not `overflow: hidden` - iOS Safari ignores `overflow: hidden`
      on the body for touch scrolling, which is the platform that needs this most.
   2. Pinning the body throws away the scroll position, so save it and restore it, or the
      page jumps to the top on open and stays there on close.
   3. Pinning removes the scrollbar, so reserve its width as paddingRight or every
      fixed-width element on the page shifts sideways the instant an overlay opens.
      (Phones use overlay scrollbars and compute 0, so the same line is right at both ends.)
   4. DERIVE the lock from the DOM - never reference-count it. A counter desyncs the first
      time two overlays close at once (Escape closes every one) or a new close path forgets
      its unlock, and the symptom of that desync is a permanently frozen page, which is
      worse than the bug being fixed.

   Visibility, not just the selector, decides. `_record_modal.html`'s two hand-rolled modals
   sit in the DOM permanently as `.modal` hidden by inline display, so a bare `.modal` match
   would lock the page on nearly every page load and never let go.
   Matching on the inline style instead is not a fix: JS hiding serializes as
   `display: none` WITH a space, so a `[style*="display:none"]` test silently stops working.

   The same visibility gate is why the guide popovers need no `:not([hidden])` clause -
   `hidden` computes to `display: none`, and `.tag-filter-panel` is toggled through inline
   display rather than the attribute at all, so both shapes are covered by the plain class.

   `.menu-scrim` is absent on purpose - `base.html::setOpen()` is the single mutator for both
   it and the drawer it dims, so `.topnav-menu.open` already answers for that surface.

   Deliberate exemption: a dropdown attached to a text input (#guide-search-dropdown) opens
   on focus rather than on a deliberate "open this" click, and freezing the page while
   someone types in a filter box would be surprising. The sidebar's
   Setup flyout (base.html) opens on hover; hover surfaces are not overlays. */
const OVERLAY_SEL = '.modal, .menu.open, .lightbox.show, .topnav-menu.open, ' +
                    '.guide-pop, .tag-filter-panel';
let scrollLockY = 0;

function syncScrollLock() {
  const wantLock = Array.from(document.querySelectorAll(OVERLAY_SEL))
    .some(el => el.getClientRects().length > 0);
  const isLocked = document.body.classList.contains('scroll-locked');
  if (wantLock === isLocked) return;
  if (wantLock) {
    scrollLockY = window.scrollY;
    const sbw = window.innerWidth - document.documentElement.clientWidth;
    document.body.style.top = `-${scrollLockY}px`;
    if (sbw > 0) document.body.style.paddingRight = `${sbw}px`;
    document.body.classList.add('scroll-locked');
  } else {
    document.body.classList.remove('scroll-locked');
    document.body.style.top = '';
    document.body.style.paddingRight = '';
    window.scrollTo(0, scrollLockY);
  }
}

// True when an overlay other than the mobile nav drawer is visibly open. The drawer is the
// outermost overlay on mobile - anything opened over it owns Escape first - so base.html's
// drawer Escape handler stands down whenever this is true. Derived from OVERLAY_SEL rather
// than a second hand-written list, so a new overlay is covered by adding it in one place,
// and gated on visibility for the same reason syncScrollLock is: four hand-rolled modals sit
// in the DOM permanently, hidden by inline display, so a bare selector match is always true.
function overlayOpenAboveDrawer() {
  return Array.from(document.querySelectorAll(OVERLAY_SEL))
    .some(el => !el.classList.contains('topnav-menu') && el.getClientRects().length > 0);
}

// Modal builder (DESIGN.md 3.12 anatomy). Returns the overlay element; call
// .remove() on it (or use the returned close()) to dismiss programmatically.
//   buildModal({ title, body, footer: [{label, class, onClick}], dismissable })
// body: Node or HTML string. footer onClick(closeFn); returning false keeps
// the modal open (e.g. validation failure). dismissable=false disables the
// Esc/backdrop/× close paths for destructive-confirm flows.
function buildModal({ title = '', body = '', footer = [], dismissable = true, onClose = null,
                      panelClass = '', footNote = '' } = {}) {
  const overlay = document.createElement('div');
  overlay.className = 'modal';
  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  const panel = document.createElement('div');
  panel.className = `modal-panel${panelClass ? ' ' + panelClass : ''}`;

  const head = document.createElement('div');
  head.className = 'modal-head';
  const h = document.createElement('h2');
  h.textContent = title;
  head.appendChild(h);

  const bodyEl = document.createElement('div');
  bodyEl.className = 'modal-body';
  if (body instanceof Node) bodyEl.appendChild(body);
  else bodyEl.innerHTML = body;

  const close = () => {
    document.removeEventListener('keydown', onKey);
    overlay.remove();
    syncScrollLock();
    if (onClose) onClose();
  };
  const onKey = (e) => { if (e.key === 'Escape' && dismissable) close(); };

  if (dismissable) {
    const x = document.createElement('button');
    x.className = 'modal-close';
    x.innerHTML = '&#x2715;';
    x.addEventListener('click', close);
    head.appendChild(x);
    backdrop.addEventListener('click', close);
    document.addEventListener('keydown', onKey);
  }

  panel.appendChild(head);
  panel.appendChild(bodyEl);
  if (footer.length) {
    const foot = document.createElement('div');
    foot.className = 'modal-foot';
    if (footNote) {
      // Left-aligned note carrying the blocking reason, so "why is Create disabled" is
      // answered beside the disabled button rather than only up in the body.
      const note = document.createElement('span');
      note.className = 'modal-foot-note';
      foot.appendChild(note);
      overlay.footNote = note;
    }
    footer.forEach(({ label, class: cls = 'btn', onClick = null }) => {
      const b = document.createElement('button');
      b.className = cls;
      b.textContent = label;
      b.addEventListener('click', () => {
        if (onClick && onClick(close) === false) return;
        if (!onClick) close();
      });
      foot.appendChild(b);
    });
    panel.appendChild(foot);
  }
  overlay.appendChild(backdrop);
  overlay.appendChild(panel);
  document.body.appendChild(overlay);
  syncScrollLock();
  overlay.closeModal = close;
  return overlay;
}

// ── Section layout ("Customize sections") control ───────────────────────────────────
// Persisted show/hide + move-up/move-down over a page's sections, shared by the channel,
// account and group detail pages (three near-identical ~78-line copies before this). A
// generated [data-section] stylesheet reorders/hides sections, so no page DOM is ever
// rebuilt and every other script's references to those nodes stay valid.
//   initSectionLayout({ config, saveUrl, names, note })
// config: the page's boot object - reads config.sections (every valid key, in the
// server-side default order) and config.sectionPref ({order, hidden} or null/undefined).
// saveUrl: the page's own POST endpoint for the pref (pages build this differently - a
// static path, a server-supplied url, or one keyed on page state - so it stays the
// caller's concern).
// names: { key: label }. A section absent from the map falls back to its own key rather
// than rendering "undefined".
// note: extra HTML appended after the modal's fixed intro sentence, for whatever a page
// needs to say beyond "this is saved server-side" (e.g. which sections always show).
// Returns { open } - open() shows the "Customize sections" modal.
function initSectionLayout({ config, saveUrl, names, note = '' }) {
  let secState = (config.sectionPref && Array.isArray(config.sectionPref.order))
    ? { order: config.sectionPref.order.slice(), hidden: (config.sectionPref.hidden || []).slice() }
    : { order: config.sections.slice(), hidden: [] };
  // A layout saved before a section existed puts that section at its server-side default
  // index, not at the end - appending would bury every new section below the last one for
  // anyone who had ever saved a layout.
  config.sections.forEach((s, i) => { if (!secState.order.includes(s)) secState.order.splice(i, 0, s); });
  secState.order = secState.order.filter((s) => config.sections.includes(s));

  const secStyle = document.createElement('style');
  document.head.appendChild(secStyle);

  function applySections() {
    secStyle.textContent = secState.order.map((s, i) =>
      `[data-section="${s}"] { order: ${i + 1}; ${secState.hidden.includes(s) ? 'display: none;' : ''} }`
    ).join('\n');
  }

  function saveSections() {
    jsonFetch(saveUrl, {
      method: 'POST', body: JSON.stringify({ value: secState }),
    }).catch(() => showToast('Could not save the section layout.', { type: 'error' }));
  }

  function open() {
    const body = document.createElement('div');
    const draw = () => {
      body.innerHTML =
        '<p class="text-muted small">Which sections this page shows, and in what order. Saved to the ' +
        `server, so it follows you across browsers. ${note}</p>` +
        secState.order.map((s, i) =>
          `<div class="seclayout-row"><label><input type="checkbox" data-sec="${s}"` +
          `${secState.hidden.includes(s) ? '' : ' checked'}> ${escHtml(names[s] || s)}</label>` +
          `<span class="seclayout-mv"><button data-secup="${s}"${i === 0 ? ' disabled' : ''} aria-label="Move up">&#9650;</button>` +
          `<button data-secdn="${s}"${i === secState.order.length - 1 ? ' disabled' : ''} aria-label="Move down">&#9660;</button></span></div>`
        ).join('');
    };
    draw();
    body.addEventListener('change', (e) => {
      const s = e.target.getAttribute('data-sec');
      if (!s) return;
      secState.hidden = e.target.checked ? secState.hidden.filter((x) => x !== s) : secState.hidden.concat([s]);
      applySections(); saveSections(); draw();
    });
    body.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b || b.disabled) return;
      const s = b.getAttribute('data-secup') || b.getAttribute('data-secdn');
      if (!s) return;
      const i = secState.order.indexOf(s);
      const j = b.hasAttribute('data-secup') ? i - 1 : i + 1;
      if (j < 0 || j >= secState.order.length) return;
      [secState.order[i], secState.order[j]] = [secState.order[j], secState.order[i]];
      applySections(); saveSections(); draw();
    });
    buildModal({
      title: 'Customize sections',
      body,
      footer: [
        { label: 'Reset', class: 'btn', onClick: () => {
          secState = { order: config.sections.slice(), hidden: [] };
          applySections(); saveSections(); draw();
          return false;
        } },
        { label: 'Done', class: 'btn btn-primary' },
      ],
    });
  }

  applySections();
  return { open };
}

// ── Delete-recording confirm body ────────────────────────────────────────────────────
// Shared by every surface that deletes a recording (the detail page's kebab, the
// recordings list's kebab) so the toggle and its consequence sentence cannot drift apart
// between them. Confirm anatomy per DESIGN.md section 4: sentence 1 says what will happen,
// sentence 2 states the destructive consequence - and here sentence 2 is a function of the
// toggle, since flipping it off makes the action non-destructive to the files.
//
// The switch ships CHECKED, so a caller that ignores it, and a user who just hits Delete,
// both get today's behavior (dev/changelog/587).
const DELETE_FILES_ON = 'Its recorded files will be permanently deleted from disk. This cannot be undone.';
const DELETE_FILES_OFF = 'Its recorded files will be left on disk. Only the recording and its history are removed from ChannelBin.';

function deleteRecordingBody(lead) {
  return `<p style="font-size:.9rem">${lead}</p>` +
    '<label style="display:flex;align-items:center;justify-content:space-between;gap:1rem;' +
      'margin:.9rem 0;font-size:.9rem;cursor:pointer">' +
      '<span>Delete the files from disk</span>' +
      '<span class="switch"><input type="checkbox" id="del-files" checked><span class="knob"></span></span>' +
    '</label>' +
    `<p id="del-files-note" style="font-size:.85rem;color:var(--text-muted)">${DELETE_FILES_ON}</p>`;
}

// True unless the user turned the switch off. Absent switch (a caller that did not use
// deleteRecordingBody) reads as true - deleting the files stays the default everywhere.
function deleteRecordingWantsFiles() {
  const cb = document.getElementById('del-files');
  return cb ? cb.checked : true;
}

// One delegated listener for the whole app - the switch only ever exists inside a modal
// that was built moments ago, so there is nothing to bind at load time.
document.addEventListener('change', (e) => {
  if (e.target.id !== 'del-files') return;
  const note = document.getElementById('del-files-note');
  if (note) note.textContent = e.target.checked ? DELETE_FILES_ON : DELETE_FILES_OFF;
});

// ── Ignore-alert confirm ──────────────────────────────────────────────────────────────
// Shared by the Alert Center row kebab (alerts.js) and the nav banner's "Show details"
// modal (base.html) - the second entry point dev/changelog/615 added so the action isn't
// reachable only by finding the matching row in the Alert Center list.
function confirmIgnoreAlert(alertId, title, onDone) {
  buildModal({
    title: 'Ignore future alerts like this',
    body: `<p>Alerts matching "<strong>${escHtml(title)}</strong>" (even if a number in the ` +
          'title changes) will stop appearing here and stop being pushed. Nothing is ' +
          'deleted, and this alert is dismissed now the same way Dismiss would. You can ' +
          'undo this anytime from <strong>Manage ignored alerts</strong>.</p>',
    footer: [
      { label: 'Cancel', class: 'btn' },
      {
        label: 'Ignore future alerts',
        class: 'btn btn-primary',
        onClick: (close) => {
          close();
          jsonFetch(`/api/alerts/${alertId}/ignore`, { method: 'POST' })
            .then(() => { if (onDone) onDone(); })
            .catch((e) => showToast(`Could not ignore that alert: ${e.message}`, { type: 'error' }));
        },
      },
    ],
  });
}

// One row of the .gd-fset settings anatomy (DESIGN.md 3.12): label + explanatory meta
// on the left, the control on the right. Shared by every settings-style modal - promoted
// here from group-detail.js when the Create channel group modal needed the same rows.
//   o: { label, meta, control, sub?, full?, id?, hide? }
// `full` = a row with no control at all (its meta runs the full width). `stack` = a row
// that HAS a control but one too wide for the fixed right-hand column - a repeating input
// list, a swatch picker - so it sits under the label instead of beside it. They are not
// interchangeable: passing `full` with a control leaves that control in a 220px column
// pinned to the right edge.
function fieldRow(o) {
  const cls = `gd-field${o.sub ? ' sub' : ''}${o.full ? ' full' : ''}${o.stack ? ' stack' : ''}`;
  return `<div class="${cls}"${o.id ? ` data-frow="${o.id}"` : ''}${o.hide ? ' style="display:none"' : ''}>` +
    `<div class="gd-field-left"><div class="gd-field-lbl">${o.label}</div>` +
    `<div class="gd-field-meta">${o.meta}</div></div>` +
    (o.control ? `<div class="gd-field-ctl${o.wide ? ' wide' : ''}">${o.control}</div>` : '') + '</div>';
}

// Swatches-plus-custom-hex color picker, shared by the account and tag modals
// (account-modal.js, tag-modal.js) - the two were near-identical ~45-line copies landed the
// same day. `idPrefix` ('acct-'/'tag-') namespaces the radio group name and the custom/preview
// ids so a page can never host two of these at once with colliding ids. `presets` is a list of
// {hex, label} objects - callers whose own preset data comes in a different shape (tag's
// [hex, label] tuples) normalize it before calling, rather than this helper knowing both.
function colorPickerHtml(idPrefix, color, defaultColor, presets) {
  const current = String(color || defaultColor);
  const hexes = (presets || []).map((p) => p.hex);
  const swatches = (presets || []).map((p) =>
    '<label class="color-swatch-label" title="' + escHtml(p.label) + '">' +
    `<input type="radio" name="${idPrefix}color" class="color-radio" value="${escHtml(p.hex)}"` +
    `${p.hex === current ? ' checked' : ''}>` +
    `<span class="color-swatch" style="background: ${escHtml(p.hex)}"></span>` +
    '</label>').join('');
  // Only a colour that is NOT one of the presets belongs in the custom box - otherwise
  // picking a preset would populate custom too, and both would claim the answer.
  const custom = hexes.includes(current) ? '' : current;
  return '<div class="color-picker">' + swatches +
    `<div class="color-custom"><label for="${idPrefix}color-custom">Custom:</label>` +
    `<input type="text" id="${idPrefix}color-custom" value="${escHtml(custom)}" placeholder="#rrggbb">` +
    `<span class="color-dot color-dot-lg" id="${idPrefix}color-preview" style="background: ${escHtml(current)}"></span>` +
    '</div></div>';
}

// Wires the read/preview/mutual-clear behavior for a colorPickerHtml() control already
// rendered under `body`. Returns readColor(), the current-value getter callers use at submit
// time. The custom box and the swatches are one control with two faces, so each clears the
// other - leaving both populated makes "which did I pick" unanswerable by looking.
function wireColorPicker(body, idPrefix, defaultColor) {
  const customInput = body.querySelector(`#${idPrefix}color-custom`);
  const preview = body.querySelector(`#${idPrefix}color-preview`);
  const readColor = () => {
    const typed = customInput.value.trim();
    if (typed) return typed;
    const picked = body.querySelector('.color-radio:checked');
    return picked ? picked.value : defaultColor;
  };
  const syncPreview = () => { preview.style.background = readColor(); };
  customInput.addEventListener('input', () => {
    if (customInput.value.trim()) {
      body.querySelectorAll('.color-radio').forEach((r) => { r.checked = false; });
    }
    syncPreview();
  });
  body.addEventListener('change', (e) => {
    if (!e.target.classList.contains('color-radio')) return;
    customInput.value = '';
    syncPreview();
  });
  return readColor;
}

// Client-side sort helper: reorders `parent`'s children matching `selector`
// by keyFn(el) (string or number), dir 'asc'|'desc'. Stable for equal keys.
function sortChildren(parent, selector, keyFn, dir = 'asc') {
  const items = Array.from(parent.querySelectorAll(`:scope > ${selector}`));
  const mul = dir === 'desc' ? -1 : 1;
  items
    .map((el, i) => ({ el, i, k: keyFn(el) }))
    .sort((a, b) => {
      if (a.k < b.k) return -1 * mul;
      if (a.k > b.k) return 1 * mul;
      return a.i - b.i;
    })
    .forEach(({ el }) => parent.appendChild(el));
}

// Screenshot lightbox (DESIGN.md 3.9): dimmed overlay, click anywhere closes.
function openLightbox(src, alt = 'Screenshot') {
  let lb = document.getElementById('app-lightbox');
  if (!lb) {
    lb = document.createElement('div');
    lb.id = 'app-lightbox';
    lb.className = 'lightbox';
    const img = document.createElement('img');
    img.alt = alt;
    lb.appendChild(img);
    lb.addEventListener('click', () => { lb.classList.remove('show'); syncScrollLock(); });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { lb.classList.remove('show'); syncScrollLock(); }
    });
    document.body.appendChild(lb);
  }
  lb.querySelector('img').src = src;
  lb.classList.add('show');
  syncScrollLock();
}

// In-field secret reveal (eyeball). A masked secret input renders SECRET_MASK; clicking the
// button fetches the real value from /api/settings/reveal (gated server-side to sensitive
// leaves) and toggles it back. State is read off the input's value, so no per-button flag can
// drift: value === SECRET_MASK means "currently masked" (the round-trip makes the sentinel
// unusable as a real value, so this can never false-positive on a genuine secret).
const SECRET_MASK = '********';  // duplicated from app/config.py MASK_SENTINEL - keep in sync
function _setRevealState(btn, revealed) {
  btn.classList.toggle('revealed', revealed);
  btn.setAttribute('aria-label', revealed ? 'Hide value' : 'Reveal value');
  btn.title = revealed ? 'Hide' : 'Reveal';
}
function initSecretReveal(root = document) {
  root.querySelectorAll('.secret-reveal-btn').forEach((btn) => {
    if (btn.dataset.wired) return;
    // No path means this eyeball does not serve a STORED secret back - it is a local
    // show-what-I-am-typing toggle wired by its own page (the account settings modal's
    // password, DESIGN.md §17.4). Wiring it here would fire a reveal request for a value
    // no endpoint will ever return.
    if (!btn.dataset.secretPath) return;
    btn.dataset.wired = '1';
    const field = btn.closest('.secret-field');
    const input = field && field.querySelector('input');
    if (!input) return;
    btn.addEventListener('click', async () => {
      if (input.value === SECRET_MASK) {
        btn.disabled = true;
        try {
          const data = await jsonFetch('/api/settings/reveal?path=' +
            encodeURIComponent(btn.dataset.secretPath));
          input.value = data.value || '';
          _setRevealState(btn, true);
        } catch (e) {
          showToast(e.message || 'Could not reveal value', { type: 'error' });
        } finally {
          btn.disabled = false;
        }
      } else {
        input.value = SECRET_MASK;
        _setRevealState(btn, false);
      }
    });
  });
}
// The OTHER eyeball: a local show-what-I-am-typing toggle, for a secret this app will
// never hand back out (an account's provider password - DESIGN.md §17.4). It is the same
// `.secret-field` + `.secret-reveal-btn` look with NO data-secret-path, and all it does is
// swap the input's `type`. There is no request and no endpoint behind it, which is the
// entire point: initSecretReveal above deliberately skips these, so without this they were
// each wired by hand at the two call sites (the Add-account form and the settings modal).
function initLocalReveal(root = document) {
  root.querySelectorAll('.secret-field > .secret-reveal-btn:not([data-secret-path])')
    .forEach((btn) => {
      if (btn.dataset.wired) return;
      const input = btn.closest('.secret-field').querySelector('input');
      if (!input) return;
      btn.dataset.wired = '1';
      btn.addEventListener('click', () => {
        const revealed = input.type === 'text';
        input.type = revealed ? 'password' : 'text';
        btn.classList.toggle('revealed', !revealed);
        btn.setAttribute('aria-label', revealed ? 'Show what I am typing' : 'Hide');
        btn.title = revealed ? 'Show what I am typing' : 'Hide';
        input.focus();
      });
    });
}
document.addEventListener('DOMContentLoaded', () => { initSecretReveal(); initLocalReveal(); });

// PASS/WARN/FAIL/CANCELLED/WAITING for a ChannelTest-shaped object ({status, error_detail}),
// or WAITING for null. Mirrors app/routes/channel_tests.py::_test_status_label - the two must
// stay in step, since the same rows are rendered server-side on first paint and re-rendered
// here on refresh. Callers layer their own context-only states (DISABLED, TESTING) on top.
function testStatusLabel(t) {
  if (!t) return 'WAITING';
  if (t.status === 'COMPLETED') return t.error_detail ? 'WARN' : 'PASS';
  if (t.status === 'CANCELLED') return 'CANCELLED';
  return 'FAIL';
}

// Scroll-lock backstop, registered LAST on purpose: every other listener in this file has
// already run by the time these fire, so the DOM they read reflects the close that just
// happened. A surface whose own open/close path forgets to call syncScrollLock therefore
// self-heals on the next click or keypress instead of leaving the page frozen - the failure
// mode that matters, since a stuck lock is worse than the leak it was fixing. Escape gets its
// own listener because it is the most common close path and closes every overlay at once.
document.addEventListener('click', syncScrollLock);
document.addEventListener('keydown', syncScrollLock);
