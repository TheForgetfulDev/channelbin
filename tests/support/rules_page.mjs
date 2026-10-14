/* Drives the REAL /rules page's row actions (static/js/rules.js over the shared actions in
   static/js/auto-rule-modal.js) in jsdom and prints what it observed as JSON. Called by
   tests/test_rules_page_conformance.py, which owns every assertion - this file only reports.

   Real: the markup is what the Flask app rendered for before.html and after.html, the page's
   own RULES_CONFIG script is evaluated as served, and util.js, auto-rule-modal.js and
   rules.js are the shipped files. base.html's other inline scripts do not run - they poll
   the server and are not what this is about. Faked: jsonFetch (answers per scenario),
   showToast (recorded), followHref (recorded - jsdom cannot navigate), and the page fetch
   swapFromServer makes, which answers after.html.

   argv: <fixture dir> <repo root>. The fixture dir holds before.html, after.html and
   ids.json ({busy, idle} - a rule with scheduled recordings and a paused one without). */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const BEFORE = read('before.html');
const AFTER = read('after.html');
const IDS = JSON.parse(read('ids.json'));
const JS = ['util.js', 'auto-rule-modal.js', 'rules.js']
  .map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

function boot(answers) {
  const errors = [];
  const navigations = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => {
    if (/navigation|reload/i.test(e.message)) navigations.push(e.message);
    else errors.push(`jsdomError: ${e.message}`);
  });
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
  const dom = new JSDOM(BEFORE, { runScripts: 'outside-only', pretendToBeVisual: true,
                                  url: 'http://localhost:5000/rules', virtualConsole: vc });
  const w = dom.window;
  const pageFetches = [];
  w.fetch = (url) => {
    pageFetches.push(String(url));
    return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(AFTER) });
  };
  w.eval(JS[0]);
  w.OPENED = [];
  w.eval('followHref = function (e, url) { OPENED.push(url); };');
  const cfg = Array.from(w.document.querySelectorAll('script:not([src])'))
    .find((s) => s.textContent.includes('window.RULES_CONFIG'));
  w.eval(cfg.textContent);
  w.eval(JS[1]);
  w.eval(JS[2]);
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
  return { w, errors, navigations, pageFetches };
}

const settle = async (w) => {
  for (let i = 0; i < 4; i += 1) await new Promise((r) => w.setTimeout(r, 0));
};
const $ = (w, sel) => w.document.querySelector(sel);
const $$ = (w, sel) => Array.from(w.document.querySelectorAll(sel));
const act = (w, id, name) => $(w, `.row[data-rule="${id}"] [data-act="${name}"]`);
const text = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
const rows = (w) => $$(w, '.rl-list .row[data-rule]').map((r) => Number(r.dataset.rule));

function dialog(w) {
  const m = $(w, '.modal');
  if (!m) return null;
  return {
    title: text(m.querySelector('.modal-head h2')),
    text: text(m.querySelector('.modal-body')),
    choices: Array.from(m.querySelectorAll('input[name="rd-fate"]'))
      .map((r) => ({ value: r.value, checked: r.checked,
                     label: text(r.closest('label').querySelector('strong')) })),
    footer: Array.from(m.querySelectorAll('.modal-foot .btn')).map((b) => [b.textContent, b.className]),
    focused: w.document.activeElement ? w.document.activeElement.textContent : null,
    error_shown: m.querySelector('#rd-err') ? !m.querySelector('#rd-err').hidden : null,
    error: text(m.querySelector('#rd-err')),
  };
}

function finish(ctx) {
  const { w, errors, navigations, pageFetches } = ctx;
  return {
    dialog_after: dialog(w),
    sent: w.SENT,
    toasts: w.TOASTS,
    rows_after: rows(w),
    head_after: text($(w, '.page-head h1')),
    page_fetches: pageFetches,
    navigations,
    errors,
  };
}

async function deleteScenario(id, answer, pickCancel) {
  const ctx = boot({ [`DELETE /api/auto-rules/${id}`]: answer });
  const { w } = ctx;
  act(w, id, 'delete').click();
  await settle(w);
  const shown = dialog(w);
  if (pickCancel) {
    const r = $(w, 'input[name="rd-fate"][value="cancel"]');
    r.checked = true;
    r.dispatchEvent(new w.Event('change', { bubbles: true }));
  }
  $(w, '.modal-foot .btn-danger').click();
  await settle(w);
  return Object.assign({ dialog: shown }, finish(ctx));
}

async function clickScenario(id, name, answers) {
  const ctx = boot(answers);
  act(ctx.w, id, name).click();
  await settle(ctx.w);
  return finish(ctx);
}

function clickAt(target) {
  const ctx = boot({});
  const el = target(ctx.w);
  el.dispatchEvent(new ctx.w.MouseEvent('click', { bubbles: true, button: 0 }));
  return { opened: ctx.w.OPENED[0] || null, errors: ctx.errors, navigations: ctx.navigations };
}

const { busy, idle } = IDS;
const EV = (o) => Object.assign({ queued: false, rule_id: busy, scheduled: 0, skipped: {},
                                  error: null, abandoned: false,
                                  guide_reach: '2026-10-16T23:00:00' }, o);
const out = {
  first_rows: rows(boot({}).w),
  row_click: clickAt((w) => $(w, `.row[data-rule="${busy}"] .rl-cell.rl-next`)),
  kebab_click: clickAt((w) => $(w, `.row[data-rule="${busy}"] [data-menu]`)),
  delete_keep: await deleteScenario(busy, { success: true, cancelled: 0, kept: 2 }, false),
  delete_cancel: await deleteScenario(busy, { success: true, cancelled: 2, kept: 1 }, true),
  delete_none: await deleteScenario(idle, { success: true, cancelled: 0, kept: 0 }, false),
  delete_fail: await deleteScenario(busy, { reject: 'Rule not found.' }, false),
  pause: await clickScenario(busy, 'pause', {
    [`POST /api/auto-rules/${busy}/pause`]: { success: true } }),
  pause_fail: await clickScenario(busy, 'pause', {
    [`POST /api/auto-rules/${busy}/pause`]: { reject: 'Rule not found.' } }),
  resume: await clickScenario(idle, 'resume', {
    [`POST /api/auto-rules/${idle}/resume`]: { success: true, evaluation: EV({
      rule_id: idle, scheduled: 2, skipped: { skipped_repeat: 1 } }) } }),
  resume_queued: await clickScenario(idle, 'resume', {
    [`POST /api/auto-rules/${idle}/resume`]: { success: true, evaluation: {
      queued: true, waiting_for: 'an account sync is already running (Acct)' } } }),
  check: await clickScenario(busy, 'check', {
    [`POST /api/auto-rules/${busy}/check`]: { success: true, evaluation: EV({
      scheduled: 1, already_scheduled: 2 }) } }),
};
{
  const { w } = boot({});
  act(w, busy, 'edit').click();
  const m = $(w, '.modal');
  out.edit = {
    title: text(m.querySelector('.modal-head h2')),
    name: m.querySelector('#ar-name').value,
    search: Array.from(m.querySelectorAll('.ar-searchbox .srow'))
      .map((r) => `${text(r.querySelector('.sk'))}: ${text(r.querySelector('.sv'))}`),
    edit_search_href: m.querySelector('.ar-searchbox-foot a').getAttribute('href'),
  };
}
process.stdout.write(JSON.stringify(out));
