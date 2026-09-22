/* Drives the REAL Maintenance page's search-index card in jsdom and prints what it
   observed as JSON. Called by tests/test_maintenance_index_js.py, which owns every
   assertion - this file only reports. Same shape as tests/support/maintenance_restart.mjs.

   Why it exists: whether the card notices a rebuild that starts after page load is a
   decision made entirely in the browser, out of a sequence of /api/nav-status readiness
   answers over time. No Python test can see it, and the way it used to be made - start the
   poll only if a status fetch already found a rebuild in flight - meant every sync's
   closing rebuild ran invisibly (dev/changelog/1081).

   What is real: the markup the Flask route rendered, and util.js + maintenance.js evaluated
   as shipped. Faked: the network (each scenario scripts what /api/settings/search-index
   answers, in order, the last answer repeating) and setInterval, which is captured rather
   than run so a 3-second poll can be stepped one tick at a time instead of waited out.
   Readiness payloads are handed to window.__applySearchReadiness directly - the same entry
   point base.html's nav-status poll calls.

   argv: <fixture dir> <repo root>. The fixture dir holds page.html. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const JS = ['util.js', 'maintenance.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

const STATUS = '/api/settings/search-index';

/* The four readiness states /api/nav-status can report, in the server's own wording
   (app/search_index.py::search_index_readiness). STALE and BUILDING both carry
   ready:false - that pair is the whole point of the card's trigger. */
const readiness = (ready, reason) => ({
  channels: { ready, reason },
  programs: { ready, reason },
});
const R_READY = readiness(true, '');
const R_STALE = readiness(false, "the channels search index is stale - its source has "
  + "changed since it was built (watermark 'a' -> 'b')");
const R_BUILDING = readiness(false, 'the channels search index is being rebuilt right now');

/* One answer from /api/settings/search-index. */
const ixRow = (name, label, status, ready, reason) => ({
  name,
  label,
  status,
  row_count: 136130,
  duration_ms: 76000,
  error: null,
  rebuilt_at: 'Sep 21, 3:04 PM',
  ready,
  reason,
});
const answer = (status, ready, reason, rebuilding = false) => ({
  success: true,
  rebuilding,
  indexes: [
    ixRow('channels', 'Channels (names, groups, EPG titles)', status, ready, reason),
    ixRow('programs', 'Programs (upcoming airings)', status, ready, reason),
  ],
});
const A_OK = answer('OK', true, '');
const A_STALE = answer('OK', false, R_STALE.channels.reason);
const A_BUILDING = answer('BUILDING', false, R_BUILDING.channels.reason, true);

function boot({ answers = [A_OK] } = {}) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  let statusCalls = 0;
  let statusFails = false;
  const queue = answers.slice();
  let current = queue.shift();

  const respond = (payload, status = 200) => Promise.resolve({
    ok: status < 400,
    status,
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve(payload),
    text: () => Promise.resolve(JSON.stringify(payload)),
  });

  const fetchStub = (target) => {
    const href = String(target);
    if (href.includes(STATUS)) {
      statusCalls += 1;
      if (statusFails) return Promise.reject(new TypeError('Failed to fetch'));
      const out = current;
      if (queue.length) current = queue.shift();
      return respond(out);
    }
    // Everything else on this page (backups, storage, tools, the nav-status poll) answers
    // emptily - those regions are not under test and each tolerates a shapeless answer.
    return respond({});
  };

  /* Captured, not shortened: the card's poll is a setInterval and stepping it by hand is
     what makes "the poll started" and "the poll stopped" observable without racing a
     clock. base.html's own nav-status interval is captured here too and simply never
     runs, which keeps it out of the fetch counts. */
  const intervals = new Map();
  let nextId = 1;
  const cleared = [];

  const dom = new JSDOM(read('page.html'), {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost:5000/maintenance',
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = fetchStub;
      w.setInterval = (fn, ms) => {
        const id = nextId;
        nextId += 1;
        intervals.set(id, { fn, ms });
        return id;
      };
      w.clearInterval = (id) => {
        const t = intervals.get(id);
        if (t) cleared.push(t.ms);
        intervals.delete(id);
      };
    },
  });
  const { window } = dom;
  const { document } = window;
  window.scrollTo = () => {};
  JS.forEach((src) => window.eval(src));

  const $ = (s) => document.querySelector(s);
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  const ctx = {
    errors,
    wait,
    statusCalls: () => statusCalls,
    failStatus: (on) => { statusFails = on; },
    hookDefined: () => typeof window.__applySearchReadiness === 'function',
    /* The one entry point base.html uses. Returns synchronously, so the innerHTML read
       straight after it is what the hook ITSELF wrote. */
    readiness: (payload) => window.__applySearchReadiness(payload),
    applyAndSettle: async (payload) => { window.__applySearchReadiness(payload); await wait(5); },
    // The card's poll, by its interval in ms. 3000 is the only one maintenance.js creates.
    pollsAt: (ms) => Array.from(intervals.values()).filter((t) => t.ms === ms).length,
    cleared: () => cleared.slice(),
    tick: async (ms) => {
      const t = Array.from(intervals.values()).find((x) => x.ms === ms);
      if (!t) throw new Error(`no interval at ${ms}ms to tick`);
      t.fn();
      await wait(5);
    },
    rowsHtml: () => $('#search-index-rows').innerHTML,
    rowsText: () => $('#search-index-rows').textContent.replace(/\s+/g, ' ').trim(),
    btn: () => {
      const b = $('#btn-rebuild-index');
      return { disabled: b.disabled, label: b.textContent.trim() };
    },
  };
  return ctx;
}

const out = {};
const record = async (name, fn) => {
  try {
    out[name] = await fn();
  } catch (e) {
    out[name] = { error: `${e.message}\n${e.stack}` };
  }
};

/* The page's own boot load is the baseline. The first readiness payload, and any repeat of
   it, must not buy a second fetch of the same fact. */
await record('baseline', async () => {
  const c = boot();
  await c.wait(10);
  const afterBoot = c.statusCalls();
  await c.applyAndSettle(R_READY);
  const afterFirst = c.statusCalls();
  await c.applyAndSettle(R_READY);
  return {
    errors: c.errors,
    hookDefined: c.hookDefined(),
    afterBoot,
    afterFirst,
    afterRepeat: c.statusCalls(),
    polls: c.pollsAt(3000),
  };
});

/* THE case this exists for: an account sync moves the source watermark (stale) and only
   then rebuilds. `ready` is false at both steps, so a trigger watching the boolean alone
   fires on the staleness and sleeps through the rebuild. Both steps are recorded. */
await record('staleThenBuilding', async () => {
  const c = boot({ answers: [A_OK, A_STALE, A_BUILDING] });
  await c.wait(10);
  await c.applyAndSettle(R_READY);
  const base = c.statusCalls();

  await c.applyAndSettle(R_STALE);
  const afterStale = { calls: c.statusCalls(), polls: c.pollsAt(3000), text: c.rowsText() };

  await c.applyAndSettle(R_BUILDING);
  return {
    errors: c.errors,
    base,
    readyFlags: [R_STALE.channels.ready, R_BUILDING.channels.ready],
    afterStale,
    afterBuilding: {
      calls: c.statusCalls(),
      polls: c.pollsAt(3000),
      text: c.rowsText(),
      btn: c.btn(),
    },
  };
});

/* Once the rebuild ends, renderIndexRows stops its own poll exactly as it did before -
   the hook takes no part in stopping it. */
await record('pollStops', async () => {
  const c = boot({ answers: [A_OK, A_BUILDING, A_OK] });
  await c.wait(10);
  await c.applyAndSettle(R_READY);
  await c.applyAndSettle(R_BUILDING);
  const running = c.pollsAt(3000);
  await c.tick(3000);
  return {
    errors: c.errors,
    running,
    stillRunning: c.pollsAt(3000),
    cleared: c.cleared(),
    btn: c.btn(),
    text: c.rowsText(),
  };
});

/* While the poll owns the card, a further readiness change must not add a second reader. */
await record('standsOffWhilePolling', async () => {
  const c = boot({ answers: [A_OK, A_BUILDING] });
  await c.wait(10);
  await c.applyAndSettle(R_READY);
  await c.applyAndSettle(R_BUILDING);
  const whilePolling = c.statusCalls();
  await c.applyAndSettle(R_STALE);
  return {
    errors: c.errors,
    polls: c.pollsAt(3000),
    whilePolling,
    after: c.statusCalls(),
  };
});

/* The hook writes nothing itself: the rows are untouched until the loader's fetch lands. */
await record('hookWritesNothing', async () => {
  const c = boot({ answers: [A_OK, A_BUILDING] });
  await c.wait(10);
  await c.applyAndSettle(R_READY);
  const before = c.rowsHtml();
  c.readiness(R_BUILDING);
  const immediately = c.rowsHtml();
  await c.wait(5);
  return {
    errors: c.errors,
    unchangedSynchronously: immediately === before,
    changedAfterFetch: c.rowsHtml() !== before,
  };
});

/* A nav-status answer that carries no search block at all (the endpoint failed, or an
   older payload) is not a change - it is an absence, and must not be read as one. */
await record('emptyPayload', async () => {
  const c = boot();
  await c.wait(10);
  await c.applyAndSettle(R_READY);
  const base = c.statusCalls();
  await c.applyAndSettle(null);
  await c.applyAndSettle(undefined);
  return { errors: c.errors, base, after: c.statusCalls() };
});

/* A fetch the hook triggered that then fails says so, rather than leaving a stale readout
   on screen - the card's existing failure path, reached from the new trigger. */
await record('triggeredFetchFails', async () => {
  const c = boot({ answers: [A_OK, A_BUILDING] });
  await c.wait(10);
  await c.applyAndSettle(R_READY);
  c.failStatus(true);
  await c.applyAndSettle(R_BUILDING);
  return { errors: c.errors, text: c.rowsText(), polls: c.pollsAt(3000) };
});

process.stdout.write(JSON.stringify(out));
process.exit(0);
