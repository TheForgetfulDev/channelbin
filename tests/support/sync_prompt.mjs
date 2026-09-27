/* Drives account-actions.js's Sync now dialog (confirmAccountSync) in jsdom and prints what
   each scenario did as JSON. Called by tests/test_sync_prompt_js.py, which owns every
   assertion - this file only reports.

   jsonFetch is replaced after the scripts load, so every request the dialog would have sent
   is captured with its body, and one scenario answers the first request with the 409 a
   sync conflict gets, to follow the flag through "Sync anyway".

   argv: <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO] = process.argv;
const UTIL = fs.readFileSync(`${REPO}/static/js/util.js`, 'utf8');
const ACTIONS = fs.readFileSync(`${REPO}/static/js/account-actions.js`, 'utf8');

const PAGE = `<!doctype html><html><head><meta name="csrf-token" content="t">
  <meta name="display-tz" content="America/New_York"></head><body></body></html>`;

// Naive UTC, the shape the server sends, `secs` from now.
const naive = (secs) => new Date(Date.now() + secs * 1000).toISOString().slice(0, 19);
const tick = () => new Promise((r) => setTimeout(r, 0));

async function scenario({ prompt, clicks = [], toggle = false, conflictFirst = false }) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
  const dom = new JSDOM(PAGE, {
    runScripts: 'dangerously', pretendToBeVisual: true,
    url: 'http://localhost:5000/', virtualConsole: vc,
  });
  const w = dom.window;
  w.eval(UTIL);
  w.eval(ACTIONS);

  const calls = [];
  w.jsonFetch = (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    if (conflictFirst && calls.length === 1) {
      const e = new Error('Sync not started');
      e.status = 409;
      e.data = { conflicts: ['A recording is running.'] };
      return Promise.reject(e);
    }
    return Promise.resolve({ success: true, message: 'Sync started.' });
  };

  w.confirmAccountSync(7, 'Account <b>2</b>', prompt, {});
  await tick();

  const modal = w.document.querySelector('.modal');
  const sw = w.document.getElementById('sync-skip');
  const seen = {
    modal: !!modal,
    title: modal ? modal.querySelector('.modal-head h2').textContent : null,
    body_text: modal ? modal.querySelector('.modal-body').textContent : null,
    body_html: modal ? modal.querySelector('.modal-body').innerHTML : null,
    switch_checked: sw ? sw.checked : null,
    note: modal && w.document.getElementById('sync-skip-note')
      ? w.document.getElementById('sync-skip-note').textContent : null,
    link: modal && modal.querySelector('.modal-body a')
      ? modal.querySelector('.modal-body a').getAttribute('href') : null,
    buttons: modal ? [...modal.querySelectorAll('.modal-foot button')].map((b) => b.textContent) : [],
  };

  if (toggle && sw) {
    sw.checked = !sw.checked;
    sw.dispatchEvent(new w.Event('change', { bubbles: true }));
    seen.note_after_toggle = w.document.getElementById('sync-skip-note').textContent;
  }

  for (const label of clicks) {
    const btn = [...w.document.querySelectorAll('.modal-foot button')]
      .find((b) => b.textContent === label);
    if (!btn) { errors.push(`no button labelled ${label}`); break; }
    btn.click();
    await tick();
    await tick();
  }

  seen.calls = calls;
  seen.modals_left = w.document.querySelectorAll('.modal').length;
  seen.errors = errors;
  return seen;
}

const on = (over = {}) => Object.assign(
  { auto: true, next_at: naive(3 * 3600 + 12 * 60 + 30), interval_hours: 12, restart_default: true },
  over);

const out = {
  auto_off: await scenario({ prompt: on({ auto: false }) }),
  nothing_scheduled: await scenario({ prompt: on({ next_at: null }) }),
  no_prompt: await scenario({ prompt: null }),
  default_on: await scenario({ prompt: on(), clicks: ['Sync now'] }),
  default_off: await scenario({ prompt: on({ restart_default: false }), clicks: ['Sync now'] }),
  toggled_off: await scenario({ prompt: on(), toggle: true, clicks: ['Sync now'] }),
  overdue: await scenario({ prompt: on({ next_at: naive(-300) }) }),
  one_hour: await scenario({ prompt: on({ interval_hours: 1 }) }),
  cancelled: await scenario({ prompt: on(), clicks: ['Cancel'] }),
  conflict: await scenario({ prompt: on(), toggle: true, conflictFirst: true,
                             clicks: ['Sync now', 'Sync anyway'] }),
};
process.stdout.write(JSON.stringify(out, null, 2));
