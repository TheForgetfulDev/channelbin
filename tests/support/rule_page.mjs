/* Drives the REAL /rules/<id> page (static/js/rule-detail.js over the shared actions in
   static/js/auto-rule-modal.js and util.js) in jsdom and prints what it observed as JSON.
   Called by tests/test_rule_page_conformance.py, which owns every assertion.

   Real: the markup the Flask app rendered (page.html, and skip.html - the same page with
   History filtered to Skipped), the page's RULE_CONFIG script as served, and the shipped
   util.js, auto-rule-modal.js and rule-detail.js. Faked: jsonFetch (answers per scenario),
   showToast (recorded), swapFromServer (recorded, so which regions an action redraws is an
   observation), matchMedia (answers per width), and the page fetch History's swap makes,
   which answers skip.html.

   argv: <fixture dir> <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const PAGE = read('page.html');
const SKIP = read('skip.html');
const { rule: RID } = JSON.parse(read('ids.json'));
const JS = ['util.js', 'auto-rule-modal.js', 'rule-detail.js']
  .map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));
const EV = { queued: false, rule_id: RID, scheduled: 1, skipped: {}, already_scheduled: 0,
             retracked: 0, error: null, problems: null, abandoned: false,
             guide_reach: '2026-10-16T23:00:00' };

function boot({ width = 1280, answers = {}, realSwap = false } = {}) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
  const dom = new JSDOM(PAGE, {
    runScripts: 'outside-only', pretendToBeVisual: true,
    url: `http://localhost:5000/rules/${RID}`, virtualConsole: vc,
  });
  const w = dom.window;
  w.matchMedia = (query) => {
    const max = /max-width:\s*(\d+)px/.exec(query);
    return { media: query, matches: max ? width <= Number(max[1]) : false,
             addEventListener() {}, removeEventListener() {} };
  };
  const fetched = [];
  w.fetch = (url) => {
    fetched.push(String(url));
    return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(SKIP) });
  };
  w.eval(JS[0]);
  w.SWAPPED = [];
  if (!realSwap) {
    w.eval('swapFromServer = function (sel) { SWAPPED.push.apply(SWAPPED, sel); return Promise.resolve(document); };');
  }
  const cfg = Array.from(w.document.querySelectorAll('script:not([src])'))
    .find((s) => s.textContent.includes('window.RULE_CONFIG'));
  w.eval(cfg.textContent);
  w.eval(JS[1]);
  w.SENT = [];
  w.TOASTS = [];
  w.ANSWERS = answers;
  w.eval(`
    jsonFetch = function (url, opts) {
      opts = opts || {};
      SENT.push({ url: url, method: opts.method || 'GET',
                  body: opts.body ? JSON.parse(opts.body) : null });
      const a = ANSWERS[(opts.method || 'GET') + ' ' + url];
      if (!a) return Promise.reject(new Error('unexpected ' + url));
      if (a.reject) return Promise.reject(new Error(a.reject));
      return Promise.resolve(a);
    };
    showToast = function (msg, o) {
      o = o || {};
      TOASTS.push({ msg: msg, type: o.type || 'success' });
    };
  `);
  w.eval(JS[2]);
  return { w, errors, fetched };
}

const settle = async (w) => {
  for (let i = 0; i < 6; i += 1) await new Promise((r) => w.setTimeout(r, 0));
};
const $ = (w, sel) => w.document.querySelector(sel);
const $$ = (w, sel) => Array.from(w.document.querySelectorAll(sel));
const text = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
const dialog = (w) => $$(w, '.modal').find((m) => m.querySelector('#ar-name')) || null;
const done = (c, extra = {}) => Object.assign({
  sent: c.w.SENT, toasts: c.w.TOASTS, swapped: c.w.SWAPPED, errors: c.errors,
}, extra);

const out = {};
{
  const c = boot();
  const host = $(c.w, '#rule-setbar');
  out.desktop = {
    setbar_class: host.className,
    chips: $$(c.w, '#rule-setbar .gd-chip').map((b) => [
      text(b.querySelector('.gd-chip-lbl')), text(b.querySelector('.gd-chip-val')), b.dataset.focus]),
    errors: c.errors,
  };
}
{
  const c = boot({ width: 375 });
  out.phone = { setbar_class: $(c.w, '#rule-setbar').className,
                rows: $$(c.w, '#rule-setbar .srow-act').length, errors: c.errors };
}
for (const focus of ['profile', 'repeats', 'limit', 'cap']) {
  const c = boot();
  $(c.w, `#rule-setbar [data-focus="${focus}"]`).click();
  const m = dialog(c.w);
  out[`chip_${focus}`] = { title: text(m.querySelector('.modal-head h2')),
                           focused: c.w.document.activeElement.id, errors: c.errors };
}
{
  const c = boot({ answers: { [`PUT /api/auto-rules/${RID}`]: { success: true, evaluation: EV } } });
  $(c.w, '[data-section="settings"] [data-act="edit"]').click();
  const m = dialog(c.w);
  const prefill = {
    name: m.querySelector('#ar-name').value,
    limit_count: m.querySelector('#ar-limit-count').value,
    limit_hours: m.querySelector('#ar-limit-hours').value,
    cap: m.querySelector('#ar-cap').value,
    repeats: m.querySelector('#ar-repeats').checked,
  };
  const footer = Array.from(m.querySelectorAll('.modal-foot .btn')).map((b) => [b.textContent, b.className]);
  m.querySelector('#ar-cap').value = '4';
  m.querySelector('.modal-foot .btn-primary').click();
  await settle(c.w);
  out.edit_save = done(c, { prefill, footer });
}
{
  const c = boot({ answers: { [`PUT /api/auto-rules/${RID}`]: { success: true, evaluation: null } } });
  $(c.w, '[data-section="settings"] [data-act="edit"]').click();
  dialog(c.w).querySelector('.modal-foot .btn-primary').click();
  await settle(c.w);
  out.edit_paused = done(c);
}
{
  const c = boot({ answers: { [`POST /api/auto-rules/${RID}/pause`]: { success: true } } });
  $(c.w, '#rule-actionbar [data-act="pause"]').click();
  await settle(c.w);
  out.pause = done(c);
}
{
  const c = boot({ answers: { [`POST /api/auto-rules/${RID}/check`]: { success: true, evaluation: EV } } });
  $(c.w, '[data-section="checks"] [data-act="check"]').click();
  await settle(c.w);
  out.check = done(c);
}
{
  const c = boot({ answers: { [`DELETE /api/auto-rules/${RID}`]: { success: true, cancelled: 0, kept: 0 } } });
  $(c.w, '#rule-actionbar [data-act="delete"]').click();
  $$(c.w, '.modal .modal-foot .btn-danger').pop().click();
  await settle(c.w);
  const navigated = c.errors.some((e) => /navigation/i.test(e));
  out.delete = done(c, { navigated, errors: c.errors.filter((e) => !/navigation/i.test(e)) });
}
{
  const c = boot({ realSwap: true });
  $(c.w, '[data-hist="skip"]').click();
  await settle(c.w);
  out.hist_filter = done(c, {
    fetched: c.fetched,
    swapped: ['#rule-history'].filter(() => c.fetched.length === 1),
    address_bar: c.w.location.pathname + c.w.location.search,
    active_chip: $(c.w, '[data-hist="skip"]').className,
  });
}
{
  const c = boot({ realSwap: true });
  const ev = new c.w.MouseEvent('click', { bubbles: true, cancelable: true, ctrlKey: true });
  $(c.w, '[data-hist="skip"]').dispatchEvent(ev);
  await settle(c.w);
  // Left to the browser, which jsdom cannot be: the link's own navigation is what it tries.
  out.hist_newtab = { fetched: c.fetched, prevented: ev.defaultPrevented,
                      errors: c.errors.filter((e) => !/navigation/i.test(e)) };
}
{
  const c = boot();
  $(c.w, '#rule-actionbar [data-act="sections"]').click();
  const m = $$(c.w, '.modal').pop();
  out.sections = {
    title: text(m.querySelector('.modal-head h2')),
    rows: Array.from(m.querySelectorAll('.seclayout-row label')).map(text),
    text: text(m.querySelector('.modal-body')),
    errors: c.errors,
  };
}
process.stdout.write(JSON.stringify(out));
