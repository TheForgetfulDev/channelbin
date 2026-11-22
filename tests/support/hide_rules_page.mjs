/* Drives the REAL Hide Channels page's Account pills in jsdom and prints what it observed as
   JSON. Called by tests/test_hide_rules_page_js.py, which owns every assertion - this file
   only reports.

   What it is for: picking an account shows every rule that reaches it, counted on that
   account alone, and the tiles follow (dev/changelog/1678). All of it is client-side
   rendering from HIDE_RULES_CONFIG, so a page that drew every rule's total serves exactly the
   same markup.

   Real: the markup is what the Flask app rendered, the page's own HIDE_RULES_CONFIG script is
   evaluated as served, and util.js + hide-rules.js are the shipped files. base.html's other
   inline scripts do not run - they poll the server and are not what this is about. Nothing
   here makes a request: the pills only re-render.

   argv: <fixture dir> <repo root>. The fixture dir holds page.html and ids.json
   ({accounts: {name: id}, rules: {pattern: id}}). */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';
import { fakeLayout } from './jsdom_layout.mjs';

const [, , DIR, REPO] = process.argv;
const PAGE = fs.readFileSync(`${DIR}/page.html`, 'utf8');
const IDS = JSON.parse(fs.readFileSync(`${DIR}/ids.json`, 'utf8'));
const JS = ['util.js', 'hide-rules.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

const text = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);

function boot() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
  const dom = new JSDOM(PAGE, { runScripts: 'outside-only', pretendToBeVisual: true,
                                url: 'http://localhost:5000/channels/hide-rules', virtualConsole: vc });
  const w = dom.window;
  w.fetch = () => Promise.reject(new Error('no request expected'));
  w.matchMedia = (query) => ({ media: query, matches: false, addEventListener() {}, removeEventListener() {},
                               addListener() {}, removeListener() {} });
  fakeLayout(w);
  const tilesServed = tiles(w);
  w.eval(JS[0]);
  const cfg = Array.from(w.document.querySelectorAll('script:not([src])'))
    .find((s) => s.textContent.includes('window.HIDE_RULES_CONFIG'));
  w.eval(cfg.textContent);
  w.eval(JS[1]);
  return { w, errors, tilesServed };
}

function tiles(w) {
  const t = (id) => text(w.document.getElementById(id));
  return {
    hidden: t('hr-t-hidden'), hidden_sub: t('hr-t-hidden-s'),
    rules_on: t('hr-t-rules-on'), rules: t('hr-t-rules'),
    deferred: t('hr-t-deferred'),
    deferred_warn: w.document.getElementById('hr-t-deferred-tile').classList.contains('warn'),
    offered: t('hr-t-offered'), offered_sub: t('hr-t-offered-s'),
  };
}

function rows(w) {
  return Array.from(w.document.querySelectorAll('#rules-section tbody tr')).map((tr) => {
    const n = tr.querySelector('.rule-match-n');
    return {
      pattern: text(tr.querySelector('.rule-pattern')),
      matches: text(n),
      tip: n ? n.getAttribute('data-tip') : null,
      kept: text(tr.querySelector('.rule-deferred-note')),
    };
  });
}

function pills(w) {
  return Array.from(w.document.querySelectorAll('#scope-pills .scope-pill.active')).map(text);
}

function view(ctx) {
  return { pills: pills(ctx.w), rows: rows(ctx.w), tiles: tiles(ctx.w), errors: ctx.errors };
}

function pick(ctx, label) {
  const pill = Array.from(ctx.w.document.querySelectorAll('#scope-pills .scope-pill'))
    .find((b) => text(b) === label);
  pill.click();
}

const out = {};
const record = (name, fn) => {
  try {
    out[name] = fn();
  } catch (e) {
    out[name] = { error: `${e.message}\n${e.stack}` };
  }
};

record('boot', () => {
  const ctx = boot();
  return Object.assign(view(ctx), { tiles_served: ctx.tilesServed });
});

record('one_account', () => {
  const ctx = boot();
  pick(ctx, 'Beta');
  return view(ctx);
});

record('two_accounts', () => {
  const ctx = boot();
  pick(ctx, 'Beta');
  pick(ctx, 'Gamma');
  return view(ctx);
});

record('all_after_account', () => {
  const ctx = boot();
  pick(ctx, 'Beta');
  pick(ctx, 'All');
  return view(ctx);
});

record('account_after_all', () => {
  const ctx = boot();
  pick(ctx, 'All');
  pick(ctx, 'Alpha');
  return view(ctx);
});

record('cleared', () => {
  const ctx = boot();
  pick(ctx, 'Beta');
  pick(ctx, 'Beta');
  return view(ctx);
});

record('sorted_by_matches', () => {
  const ctx = boot();
  pick(ctx, 'Alpha');
  // The header is re-rendered on every sort, so it is looked up again each click.
  const head = () => ctx.w.document.querySelector('#rules-section th[data-sortcol="matches"]');
  head().click();
  const asc = rows(ctx.w).map((r) => r.pattern);
  head().click();
  const desc = rows(ctx.w).map((r) => r.pattern);
  return { asc, desc, errors: ctx.errors };
});

out.ids = IDS;
process.stdout.write(JSON.stringify(out));
