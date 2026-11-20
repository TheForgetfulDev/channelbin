/* Drives the Settings page's reverse-proxy check (static/js/settings.js) in jsdom and prints
   what it observed as JSON. Called by tests/test_settings_proxy_check_js.py, which owns every
   assertion - this file only reports.

   What is real: the page is what the Flask app answered for /settings, and util.js +
   settings.js are the shipped files. Faked: /api/settings/proxy-check, answered from the
   scenario's two scripted replies (the plain read and the probe), with every request's URL
   and headers recorded so the test can see what the page sent.

   argv: <page.html> <repo root> <scenarios.json>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , PAGE, REPO, SCENARIOS] = process.argv;
const HTML = fs.readFileSync(PAGE, 'utf8');
const JS = ['util.js', 'settings.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));
const scenarios = JSON.parse(fs.readFileSync(SCENARIOS, 'utf8'));

// One page for every scenario: the check keeps no state between clicks, so each scenario
// swaps the scripted replies, clears what was recorded, and clicks again.
const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
let sc = null;
const requests = [];
const answer = (payload, ok = true) => Promise.resolve({
  ok, status: ok ? 200 : 500,
  headers: { get: () => 'application/json' },
  json: () => Promise.resolve(payload),
  text: () => Promise.resolve(JSON.stringify(payload)),
});
const fetchStub = (target, opts = {}) => {
  const url = new URL(String(target), 'http://localhost:5000/settings');
  if (url.pathname !== '/api/settings/proxy-check') return answer({ success: true });
  requests.push({ query: url.search, headers: opts.headers || {} });
  if (sc.fail) return answer({ error: 'boom' }, false);
  return answer(url.searchParams.get('probe') === '1' ? sc.probe : sc.plain);
};
const dom = new JSDOM(HTML, {
  runScripts: 'dangerously', pretendToBeVisual: true,
  url: 'http://localhost:5000/settings', virtualConsole: vc,
  beforeParse(w) {
    w.fetch = fetchStub;
    w.CSS = { escape: (v) => String(v).replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`) };
    w.matchMedia = (q) => ({
      media: q, matches: false, onchange: null,
      addEventListener() {}, removeEventListener() {},
      addListener() {}, removeListener() {}, dispatchEvent() { return false; },
    });
  },
});
const { window } = dom;
window.scrollTo = () => {};
window.scrollBy = () => {};
errors.length = 0;
JS.forEach((src) => window.eval(src));
const btn = window.document.querySelector('#proxy-check-btn');
const box = window.document.querySelector('#proxy-check-result');
const firstHidden = box.hidden;

const out = { _firstHidden: firstHidden };
for (const [name, scenario] of Object.entries(scenarios)) {
  sc = scenario;
  requests.length = 0;
  errors.length = 0;
  btn.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
  await new Promise((r) => setTimeout(r, 20));
  out[name] = {
    errors: [...errors], requests: [...requests],
    hidden: box.hidden,
    tone: [...box.classList].find((c) => c.startsWith('notice-')) || '',
    text: box.textContent.replace(/\s+/g, ' ').trim(),
    disabled: btn.disabled,
  };
}
window.close();
process.stdout.write(JSON.stringify(out), () => process.exit(0));
