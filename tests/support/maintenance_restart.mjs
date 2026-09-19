/* Drives the REAL Maintenance page's restart flow in jsdom and prints what it observed
   as JSON. Called by tests/test_maintenance_restart_js.py, which owns every assertion -
   this file only reports. Same shape as tests/support/logs_page.mjs.

   Why it exists: the restart-wait modal decides "the service is back" entirely in the
   browser, from a sequence of heartbeat answers over time. No Python test can see that
   decision, and the way it used to be made - wait to catch one failed poll - silently
   never fired on a restart faster than one poll (dev/changelog/1022).

   What is real: the markup the Flask route rendered (with and without CHANNELBIN_DOCKER),
   and util.js + maintenance.js evaluated as shipped. Faked: the network (each scenario
   scripts its heartbeat answers), the clock (timers are shortened and Date.now can be
   pushed forward so the 45s slow warning is reachable in milliseconds), and page reload,
   which jsdom reports as an unimplemented navigation - that report IS the observation.

   argv: <fixture dir> <repo root>. The fixture dir holds page.html and page_docker.html. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const JS = ['util.js', 'maintenance.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

const RESTART_STATUS = '/api/settings/restart-status';
const RESTART_PARKED = '/api/settings/restart-parked';
const RESTART = '/api/settings/restart';

/* `beats` answers the heartbeat in order, the last one repeating: an instance id string,
   or null for "the connection is down". `restartAnswer` is what the POST answers: an
   object, or null for a dropped connection; `restartStatus` its HTTP status. `parked` is
   what restart-parked answers: a list of rows, or null for a failed lookup. */
function boot({
  page = 'page.html', beats = ['old'], restartAnswer = { success: true, instance_id: 'old' },
  restartStatus = 200, parked = [],
} = {}) {
  const errors = [];
  let reloads = 0;
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => {
    if (/navigation/i.test(e.message)) { reloads += 1; return; }
    errors.push(`jsdomError: ${e.message}`);
  });
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  let beat = 0;
  let clockOffset = 0;
  const heartbeats = [];

  const respond = (payload, status = 200) => Promise.resolve({
    ok: status < 400,
    status,
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve(payload),
    text: () => Promise.resolve(JSON.stringify(payload)),
  });

  const fetchStub = (target) => {
    const href = String(target);
    if (href.includes(RESTART_STATUS)) {
      const id = beats[Math.min(beat, beats.length - 1)];
      beat += 1;
      heartbeats.push(id);
      if (id === null) return Promise.reject(new TypeError('Failed to fetch'));
      return respond({ restart_needed: false, instance_id: id });
    }
    if (href.includes(RESTART_PARKED)) {
      if (parked === null) return Promise.reject(new TypeError('Failed to fetch'));
      return respond({ success: true, parked });
    }
    if (href.includes(RESTART)) {
      if (restartAnswer === null) return Promise.reject(new TypeError('Failed to fetch'));
      return respond(restartAnswer, restartStatus);
    }
    return respond({});
  };

  const dom = new JSDOM(read(page), {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost:5000/maintenance',
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = fetchStub;
      const realSetTimeout = w.setTimeout.bind(w);
      w.setTimeout = (fn, ms, ...args) => realSetTimeout(fn, Math.min(ms || 0, 2), ...args);
      const realNow = w.Date.now.bind(w.Date);
      w.Date.now = () => realNow() + clockOffset;
    },
  });
  const { window } = dom;
  const { document } = window;
  window.scrollTo = () => {};
  JS.forEach((src) => window.eval(src));

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));

  const ctx = {
    errors, heartbeats, $, $$,
    reloads: () => reloads,
    wait: (ms) => new Promise((r) => setTimeout(r, ms)),
    advanceClock: (ms) => { clockOffset += ms; },
    modalTitles: () => $$('.modal-title, .modal-head h2, .modal-head h3').map((n) => n.textContent.trim()),
    // The confirm opens after the restart-parked lookup answers, so this waits for it.
    openConfirm: async () => { click($('#btn-restart-now')); await ctx.wait(10); },
    confirmText: () => {
      const panels = $$('.modal-panel');
      return panels.length ? panels[panels.length - 1].textContent.replace(/\s+/g, ' ').trim() : '';
    },
    footButton: (label) => $$('.modal-panel button').find((b) => b.textContent.trim() === label),
    restart: async () => {
      await ctx.openConfirm();
      click(ctx.footButton('Restart now'));
      await ctx.wait(20);
    },
    statusText: () => (($('.restart-wait-status') || {}).textContent || '').trim(),
    hasReloadButton: () => !!ctx.footButton('Reload page'),
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

/* A container restart: every heartbeat answers, and the id changes. No poll ever fails. */
await record('fast', async () => {
  const c = boot({ beats: ['old', 'old', 'new'] });
  await c.restart();
  await c.wait(200);
  return { errors: c.errors, reloads: c.reloads(), status: c.statusText(), heartbeats: c.heartbeats };
});

/* The old process keeps answering: nothing reloads, and the timer still counts. */
await record('same', async () => {
  const c = boot({ beats: ['old'] });
  await c.restart();
  await c.wait(100);
  return {
    errors: c.errors,
    reloads: c.reloads(),
    status: c.statusText(),
    polls: c.heartbeats.length,
    reloadButton: c.hasReloadButton(),
  };
});

/* Past 45s with the old process still answering, the escape hatch appears even though
   no poll ever failed. */
await record('slow', async () => {
  const c = boot({ beats: ['old'] });
  await c.restart();
  await c.wait(20);
  c.advanceClock(46000);
  await c.wait(60);
  return { errors: c.errors, reloads: c.reloads(), reloadButton: c.hasReloadButton(), status: c.statusText() };
});

/* The POST's own connection dropped, so there is no old id to compare. A good heartbeat
   before any failure is the old process and must not count; good after a failure does. */
await record('fallback', async () => {
  const c = boot({ restartAnswer: null, beats: ['x', 'x', null, 'x'] });
  await c.restart();
  await c.wait(200);
  return { errors: c.errors, reloads: c.reloads(), heartbeats: c.heartbeats };
});

await record('fallbackNoDrop', async () => {
  const c = boot({ restartAnswer: null, beats: ['x'] });
  await c.restart();
  await c.wait(100);
  return { errors: c.errors, reloads: c.reloads(), polls: c.heartbeats.length };
});

/* The confirm, in and out of a container. */
await record('confirmBare', async () => {
  const c = boot();
  await c.openConfirm();
  return { errors: c.errors, text: c.confirmText() };
});

await record('confirmDocker', async () => {
  const c = boot({ page: 'page_docker.html' });
  await c.openConfirm();
  return { errors: c.errors, text: c.confirmText(), button: !!c.footButton('Restart now') };
});

/* A parked recording never makes the POST refuse, so the confirm is where it is named
   (dev/changelog/1026). */
const PARKED_ROW = {
  id: 17, name: 'Parked Game', status: 'CONVERTING',
  label: '#17 "Parked Game" is parked waiting on "Live Match", not blocking (restarting discards the 66% encoded so far)',
};
await record('confirmParked', async () => {
  const c = boot({ parked: [PARKED_ROW] });
  await c.openConfirm();
  return { errors: c.errors, text: c.confirmText(), button: !!c.footButton('Restart now') };
});

await record('confirmParkedLookupFailed', async () => {
  const c = boot({ parked: null });
  await c.openConfirm();
  return { errors: c.errors, text: c.confirmText(), button: !!c.footButton('Restart now') };
});

/* The 409: blocking rows under "Restarting kills it", parked rows apart from them. */
await record('refusedWithParked', async () => {
  const c = boot({
    restartStatus: 409,
    restartAnswer: {
      error: 'Work is in flight',
      blocking: [{ id: 3, name: 'Live Match', status: 'IN_PROGRESS', label: '#3 "Live Match" is recording now' }],
      parked: [PARKED_ROW],
    },
  });
  await c.restart();
  const lists = c.$$('.modal-panel .blocking-list').map((ul) => ul.textContent.replace(/\s+/g, ' ').trim());
  return { errors: c.errors, titles: c.modalTitles(), lists, text: c.confirmText() };
});

process.stdout.write(JSON.stringify(out));
process.exit(0);
