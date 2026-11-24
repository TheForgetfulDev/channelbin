/* Drives the name-match review page's Accept all in jsdom and prints what it observed as
   JSON. Called by tests/test_epg_findings.py, which owns every assertion - this file only
   reports.

   What is real: the page the app rendered, util.js and epg-source-review.js as shipped.
   Faked: the network. The picks endpoint answers TOTAL picks; the accept endpoint records
   each chunk and answers what it applied, the last pick counted as no longer proposed.

   argv: <page.html> <repo root> <source id> <total> <scenario: confirm | cancel |
   fail-second>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , PAGE, REPO, SOURCE, TOTAL_ARG, SCENARIO] = process.argv;
const TOTAL = Number(TOTAL_ARG);
const html = fs.readFileSync(PAGE, 'utf8');
const JS = ['util.js', 'epg-source-review.js']
  .map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));
const PAGE_URL = `http://localhost:5000/epg-sources/${SOURCE}/review?unguided=1`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const errors = [];
const requests = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
const answer = (status, body) => Promise.resolve({
  ok: status < 400, status, statusText: 'x',
  headers: { get: () => 'application/json' },
  json: () => Promise.resolve(body),
  text: () => Promise.resolve(JSON.stringify(body)),
});
let posts = 0;
const dom = new JSDOM(html, {
  runScripts: 'dangerously', pretendToBeVisual: true, url: PAGE_URL, virtualConsole: vc,
  beforeParse(w) {
    w.fetch = (target, init = {}) => {
      const url = new URL(String(target), PAGE_URL);
      const method = (init.method || 'GET').toUpperCase();
      requests.push({ path: url.pathname, query: url.search.slice(1), method, body: init.body || null });
      if (url.pathname === `/epg-sources/${SOURCE}/review`) {
        return Promise.resolve({
          ok: true, status: 200, headers: { get: () => 'text/html' },
          text: () => Promise.resolve(html),
        });
      }
      if (url.pathname === `/api/epg-sources/${SOURCE}/name-matches` && method === 'GET') {
        const picks = [];
        for (let i = 0; i < TOTAL; i += 1) picks.push({ channel_id: 1000 + i, xml_id: `id${i}` });
        return answer(200, { success: true, picks });
      }
      if (url.pathname === `/api/epg-sources/${SOURCE}/name-matches/accept`) {
        posts += 1;
        if (SCENARIO === 'fail-second' && posts === 2) {
          return answer(409, { error: 'The source is refreshing.' });
        }
        const n = JSON.parse(init.body).picks.length;
        const last = JSON.parse(init.body).picks.some((p) => p.channel_id === 1000 + TOTAL - 1);
        return answer(200, { success: true, accepted: last ? n - 1 : n, waiting: 0,
                             stale: last ? 1 : 0, message: 'x' });
      }
      return answer(404, { error: 'not faked' });
    };
  },
});
const w = dom.window;
const d = w.document;
w.scrollTo = () => {};
JS.forEach((src) => w.eval(src));

const button = () => d.querySelector('[data-act="accept-all"]');
const out = { errors, buttonBefore: button() ? button().textContent.trim() : null };
button().dispatchEvent(new w.MouseEvent('click', { bubbles: true, cancelable: true }));
await wait(50);
const panel = [...d.querySelectorAll('.modal-panel')].pop();
out.modal = panel ? panel.textContent.replace(/\s+/g, ' ').trim() : '';
const press = (label) => [...panel.querySelectorAll('button')]
  .find((b) => b.textContent.trim().startsWith(label))
  .dispatchEvent(new w.MouseEvent('click', { bubbles: true, cancelable: true }));
press(SCENARIO === 'cancel' ? 'Cancel' : 'Accept');
await wait(300);

const accepts = requests.filter((r) => r.method === 'POST');
out.picksQuery = (requests.find((r) => r.path.endsWith('/name-matches')) || {}).query;
out.chunkSizes = accepts.map((r) => JSON.parse(r.body).picks.length);
out.distinctPosted = new Set(accepts.flatMap((r) => JSON.parse(r.body).picks
  .map((p) => p.channel_id))).size;
out.toast = [...d.querySelectorAll('.toast')].map((t) => t.textContent.replace(/\s+/g, ' ').trim())
  .join(' | ');
out.buttonAfter = button() ? button().textContent.trim() : null;
out.pageReads = requests.filter((r) => r.path === `/epg-sources/${SOURCE}/review`).length;
process.stdout.write(JSON.stringify(out));
process.exit(0);
