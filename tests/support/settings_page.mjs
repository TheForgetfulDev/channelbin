/* Drives the REAL /settings page in jsdom and prints what it observed as JSON.
   Called by tests/test_settings_page_js.py, which owns every assertion - this file only
   reports. Same shape as tests/support/accounts_page.mjs.

   What it is for: the page's Basic/Advanced view and its search are two inputs to one
   filter function in static/js/settings.js (DESIGN.md 15.9, dev/changelog/1005), and
   every count, collapse, badge and notice they produce happens in the browser - the
   server renders every row either way. The same function dims a row whose gate is off
   (dev/changelog/1007), which the server never decides either.

   What is real: both pages are what the Flask app answered with the view pref unset and
   set to Advanced, and util.js + settings.js are the shipped files evaluated the way
   their <script src> would have been. Faked: the network (every POST is recorded and
   answered with success). jsdom computes no layout, so nothing here says where anything
   lands on screen.

   argv: <fixture dir> <repo root>. The fixture dir holds plain.html (a config that sets
   nothing), basic.html (three fields changed from their default, view never chosen) and
   advanced.html (the same, saved as Advanced). */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const PAGES = {
  plain: fs.readFileSync(`${DIR}/plain.html`, 'utf8'),
  basic: fs.readFileSync(`${DIR}/basic.html`, 'utf8'),
  advanced: fs.readFileSync(`${DIR}/advanced.html`, 'utf8'),
};
const JS = ['util.js', 'settings.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

function boot(start, { query = '', changed = false } = {}) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  const posts = [];
  const fetchStub = (target, opts = {}) => {
    const url = new URL(String(target), 'http://localhost:5000/settings');
    if ((opts.method || 'GET').toUpperCase() === 'POST') {
      posts.push({ path: url.pathname, body: JSON.parse(opts.body || 'null') });
    }
    // A field save answers the way /api/settings/field does for a value set back to its
    // default, so a scenario can watch the mark clear.
    // Validate answers from the text it was sent: "bad" in it earns one error whose
    // message carries markup (it must reach the page as text) and one warning; anything
    // else is clean.
    const body = JSON.parse(opts.body || 'null') || {};
    const problems = String(body.text || '').includes('bad')
      ? [{ severity: 'error', path: 'recording', line: 2, message: 'Should be <b>a section</b>.' },
         { severity: 'warning', path: 'bogus', line: 5, message: 'Not a setting.' }]
      : [];
    const payload = url.pathname === '/api/settings/field'
      ? { success: true, restart_required: false, changed_from_default: false }
      : url.pathname === '/api/settings/validate'
        ? { success: true, valid: !problems.length, problems }
        : { success: true };
    return Promise.resolve({
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)),
    });
  };

  const dom = new JSDOM(PAGES[start], {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: `http://localhost:5000/settings${query ? `?q=${encodeURIComponent(query)}` : ''}${changed ? '?changed=1' : ''}`,
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = fetchStub;
      // jsdom has no CSS.escape; every id this page escapes is plain [a-z_-].
      w.CSS = { escape: (v) => String(v).replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`) };
      w.matchMedia = (q) => ({
        media: q, matches: false, onchange: null,
        addEventListener() {}, removeEventListener() {},
        addListener() {}, removeListener() {}, dispatchEvent() { return false; },
      });
    },
  });
  const { window } = dom;
  const { document } = window;
  window.scrollTo = () => {};
  window.scrollBy = () => {};
  // Only errors from here on are the page's own: base.html's inline scripts run during
  // parse, before the shipped util.js has been evaluated, exactly as they do in every
  // other harness in this directory.
  errors.length = 0;
  JS.forEach((src) => window.eval(src));

  const $ = (s) => document.querySelector(s);
  const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
  const text = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
  const c = {
    window, document, errors, posts, $,
    settle: () => new Promise((r) => setTimeout(r, 30)),
    // Sets a control the way a user does: the value first, then the change event.
    set: (path, value) => {
      const el = $(`[data-setting-path="${path}"]`);
      if (el.type === 'checkbox') el.checked = value;
      else el.value = value;
      el.dispatchEvent(new window.Event('change', { bubbles: true }));
    },
    click: (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })),
    search: (q) => {
      const el = $('#ssearch');
      el.value = q;
      el.dispatchEvent(new window.Event('input', { bubbles: true }));
    },
    snap: () => {
      const rows = $$('.frow[data-path]');
      const shown = rows.filter((r) => !r.classList.contains('off'));
      const sections = {};
      $$('.sec-card').forEach((sec) => {
        const id = sec.dataset.sec;
        const more = $(`[data-more="${id}"]`);
        sections[id] = {
          collapsed: sec.classList.contains('nohit'),
          head: text($(`[data-cnt="${id}"]`)),
          rail: text($(`[data-rc="${id}"]`)),
          railDim: $(`[data-rail="${id}"]`).classList.contains('nohit'),
          more: more && !more.hidden ? text(more) : null,
          hiddenGroups: $$('[data-sub]', sec).filter((g) => g.hidden).map((g) => g.dataset.sub),
          hiddenUnits: $$('[data-tier]:not(.frow)', sec).filter((u) => u.hidden).length,
        };
      });
      return {
        view: document.querySelector('.set-page').dataset.view,
        ready: document.querySelector('.set-page').classList.contains('ready'),
        checked: $$('[data-view-set]').filter((b) => b.getAttribute('aria-checked') === 'true')
          .map((b) => b.dataset.viewSet),
        on: $$('[data-view-set].on').map((b) => b.dataset.viewSet),
        shownPaths: shown.map((r) => r.dataset.path),
        shownAdvancedHits: shown.filter((r) => r.dataset.tier === 'advanced' && r.classList.contains('hit'))
          .map((r) => r.dataset.path),
        placeholder: $('#ssearch').placeholder,
        chip: text($('#shits')),
        notice: text($('#xtier-slot .xtier')),
        noticeAction: text($('#xtier-slot [data-view-go]')),
        pickerCount: text($('#sp-n')),
        changedPaths: rows.filter((r) => r.dataset.changed === 'true').map((r) => r.dataset.path),
        changedChip: {
          active: $('#schanged').classList.contains('active'),
          pressed: $('#schanged').getAttribute('aria-pressed'),
          disabled: $('#schanged').disabled,
          count: text($('#schanged-n')),
        },
        verdict: text($('#no-hits-slot')),
        // Every gated row's gate badge, keyed by path (null while it is hidden); a row whose
        // gates all hold is absent.
        gated: Object.fromEntries(rows.filter((r) => r.classList.contains('gated'))
          .map((r) => { const b = r.querySelector('.gate-badge'); return [r.dataset.path, b.hidden ? null : text(b)]; })),
        emptyLinesWhenUngated: $$('.frow[data-gated-by]:not(.gated) .gate-badge')
          .every((b) => b.hidden && b.textContent === ''),
        gatedHits: rows.filter((r) => r.classList.contains('gated') && r.classList.contains('hit'))
          .map((r) => r.dataset.path),
        url: window.location.search,
        sections,
      };
    },
  };
  return c;
}

const out = {};
const record = async (name, fn) => {
  let c = null;
  try {
    out[name] = await fn((...a) => { c = boot(...a); return c; });
    out[name].errors = c ? c.errors : [];
  } catch (e) {
    out[name] = { error: `${e.message}\n${e.stack}` };
  } finally {
    if (c) c.window.close();
  }
};

/* ── Never chosen: the page opens in Basic ──────────────────────────── */
await record('basic_boot', async (start) => {
  const c = start('basic');
  await c.settle();
  return { ...c.snap(), posts: c.posts };
});

/* ── A search in Basic that reaches Advanced rows ───────────────────── */
await record('basic_search', async (start) => {
  const c = start('basic');
  c.search('stall');
  const during = c.snap();
  c.search('');
  const cleared = c.snap();
  return { during, cleared, posts: c.posts };
});

/* ── The notice's own action keeps what the search found ────────────── */
await record('notice_switch', async (start) => {
  const c = start('basic');
  c.search('stall');
  c.click(c.$('#xtier-slot [data-view-go]'));
  const after = c.snap();
  c.search('');
  const cleared = c.snap();
  await c.settle();
  return { after, cleared, posts: c.posts };
});

/* ── A card footer's action ─────────────────────────────────────────── */
await record('footer_switch', async (start) => {
  const c = start('basic');
  c.click(c.$('[data-more="recording"] [data-view-go]'));
  await c.settle();
  return { ...c.snap(), posts: c.posts };
});

/* ── Saved as Advanced, then switched back ──────────────────────────── */
await record('advanced_boot', async (start) => {
  const c = start('advanced');
  const opened = c.snap();
  c.click(c.$('[data-view-set="basic"]'));
  const switched = c.snap();
  // Clicking the view already showing changes nothing and saves nothing.
  c.click(c.$('[data-view-set="basic"]'));
  await c.settle();
  return { opened, switched, posts: c.posts };
});

/* ── A /settings?q= deep link into an Advanced setting, in Basic ────── */
await record('deep_link', async (start) => {
  const c = start('basic', { query: 'early_fail_abort_count' });
  await c.settle();
  return c.snap();
});

/* ── The changed-from-default chip, in Basic ────────────────────────── */
await record('changed_chip', async (start) => {
  const c = start('basic');
  const before = c.snap();
  c.click(c.$('#schanged'));
  const on = c.snap();
  c.search('poll');
  const withSearch = c.snap();
  c.search('');
  c.click(c.$('#schanged'));
  const off = c.snap();
  await c.settle();
  return { before, on, withSearch, off, posts: c.posts };
});

/* ── Saving a changed field back to its default, with the chip on ───── */
await record('changed_save', async (start) => {
  const c = start('basic');
  c.click(c.$('#schanged'));
  const input = c.$('[data-setting-path="recording.retention_days"]');
  input.value = '0';
  input.dispatchEvent(new c.window.Event('blur'));
  await c.settle();
  return { ...c.snap(), posts: c.posts };
});

/* ── A reload with ?changed=1 lands on the same list ────────────────── */
await record('changed_reload', async (start) => {
  const c = start('basic', { changed: true });
  await c.settle();
  return c.snap();
});

/* ── ?changed=1 on a config that changes nothing ────────────────────── */
await record('changed_none_reload', async (start) => {
  const c = start('plain', { changed: true });
  const opened = c.snap();
  c.click(c.$('#nh-changed-off'));
  return { opened, cleared: c.snap() };
});

/* ── A config that changes nothing ──────────────────────────────────── */
await record('changed_none', async (start) => {
  const c = start('plain');
  const opened = c.snap();
  c.click(c.$('#schanged'));
  return { opened, clicked: c.snap() };
});

/* ── Gated rows follow their gating controls live ───────────────────── */
await record('gates', async (start) => {
  const c = start('plain');
  const opened = c.snap();
  const PP = 'recording.post_process';
  // Read before the save answers: the dim follows the control, not the response.
  c.set(`${PP}.enabled`, false);
  const ppOff = c.snap();
  c.set(`${PP}.enabled`, true);
  const ppOn = c.snap();
  c.set(`${PP}.format`, 'mkv');
  const mkv = c.snap();
  c.set(`${PP}.reencode_mode`, 'never');
  const mkvNever = c.snap();
  c.set(`${PP}.format`, 'mp4');
  const never = c.snap();
  c.search('crf');
  const searched = c.snap();
  c.search('');
  c.set(`${PP}.reencode_mode`, 'damaged');
  c.set(`${PP}.auto_restart`, false);
  c.set(`${PP}.collision_policy`, 'off');
  const restartAndCollision = c.snap();
  c.set('recording.move_on_complete.enabled', true);
  const moveOn = c.snap();
  await c.settle();
  return { opened, ppOff, ppOn, mkv, mkvNever, never, searched, restartAndCollision, moveOn };
});

/* ── The config.yaml tab's Validate button ──────────────────────────── */
await record('yaml_validate', async (start) => {
  const c = start('plain');
  const area = c.$('#pane-yaml textarea');
  const region = () => ({
    text: c.$('#yaml-check').textContent.replace(/\s+/g, ' ').trim(),
    badge: c.$('#yaml-check .badge')?.className || null,
    items: Array.from(c.document.querySelectorAll('#yaml-check li')).map((li) => li.className),
    markup: c.$('#yaml-check').querySelector('b') !== null,
  });
  const opened = region();
  area.value = 'recording: bad\n';
  c.click(c.$('#yaml-validate'));
  await c.settle();
  const bad = region();
  area.dispatchEvent(new c.window.Event('input', { bubbles: true }));
  const edited = region();
  area.value = 'recording: {}\n';
  c.click(c.$('#yaml-validate'));
  await c.settle();
  const good = region();
  return { opened, bad, edited, good, posts: c.posts };
});

// base.html's polls leave timers armed in every window, so node would never exit on its own.
process.stdout.write(JSON.stringify(out), () => process.exit(0));
