/* Drives the REAL Maintenance page's backup list in jsdom and prints what it observed as
   JSON. Called by tests/test_maintenance_backups_js.py, which owns every assertion - this
   file only reports. Same shape as tests/support/maintenance_index.mjs.

   Why it exists: whether the backup list re-reads itself, and when it holds off, is decided
   entirely in maintenance.js from a timer, the tab's visibility and the <select>'s own
   value (dev/changelog/1356). No Python test can see any of it.

   What is real: the markup the Flask route rendered and util.js + maintenance.js evaluated
   as shipped. Faked: the network (the backup list answers one fixed backup, everything
   else answers emptily), setInterval (captured, stepped by hand) and document.hidden.

   argv: <fixture dir> <repo root>. The fixture dir holds page.html. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const JS = ['util.js', 'maintenance.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

const BACKUPS = '/api/settings/backups';
const BACKUP_LIST = [{ filename: 'config-20261030.yaml', created_at: 'Oct 30, 2026 03:00 AM' }];

function boot() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  let backupCalls = 0;
  const respond = (payload) => Promise.resolve({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve(payload),
    text: () => Promise.resolve(JSON.stringify(payload)),
  });
  const fetchStub = (target) => {
    if (String(target).includes(BACKUPS)) {
      backupCalls += 1;
      return respond(BACKUP_LIST);
    }
    return respond({});
  };

  const intervals = new Map();
  let nextId = 1;
  let hidden = false;

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
      w.clearInterval = (id) => { intervals.delete(id); };
      Object.defineProperty(w.document, 'hidden', { get: () => hidden, configurable: true });
    },
  });
  const { window } = dom;
  const { document } = window;
  window.scrollTo = () => {};
  JS.forEach((src) => window.eval(src));

  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const sel = () => document.querySelector('#backup-select');

  return {
    errors,
    wait,
    backupCalls: () => backupCalls,
    intervalsMs: () => Array.from(intervals.values()).map((t) => t.ms).sort((a, b) => a - b),
    setHidden: (on) => { hidden = on; },
    select: (value) => {
      sel().value = value;
      sel().dispatchEvent(new window.Event('change'));
    },
    selected: () => sel().value,
    options: () => Array.from(sel().options).map((o) => o.value),
    tick: async (ms) => {
      const t = Array.from(intervals.values()).find((x) => x.ms === ms);
      if (!t) throw new Error(`no interval at ${ms}ms to tick`);
      t.fn();
      await wait(5);
    },
  };
}

const MINUTE = 60 * 1000;
const out = {};
const record = async (name, fn) => {
  try {
    out[name] = await fn();
  } catch (e) {
    out[name] = { error: `${e.message}\n${e.stack}` };
  }
};

/* The list is read once at boot, and a tick in a visible tab with nothing selected reads
   it again. */
await record('visibleTick', async () => {
  const c = boot();
  await c.wait(10);
  const afterBoot = c.backupCalls();
  await c.tick(MINUTE);
  return { errors: c.errors, intervals: c.intervalsMs(), afterBoot, afterTick: c.backupCalls(),
           options: c.options() };
});

/* A hidden tab is not re-read; becoming visible again, the next tick is. */
await record('hiddenTick', async () => {
  const c = boot();
  await c.wait(10);
  const afterBoot = c.backupCalls();
  c.setHidden(true);
  await c.tick(MINUTE);
  const whileHidden = c.backupCalls();
  c.setHidden(false);
  await c.tick(MINUTE);
  return { errors: c.errors, afterBoot, whileHidden, afterVisible: c.backupCalls() };
});

/* A backup the user has picked is not dropped by a tick: the list holds while one is
   selected, and resumes once the choice is cleared. */
await record('selectionHolds', async () => {
  const c = boot();
  await c.wait(10);
  const afterBoot = c.backupCalls();
  c.select(BACKUP_LIST[0].filename);
  await c.tick(MINUTE);
  const whileSelected = { calls: c.backupCalls(), selected: c.selected() };
  c.select('');
  await c.tick(MINUTE);
  return { errors: c.errors, afterBoot, whileSelected, afterCleared: c.backupCalls() };
});

process.stdout.write(JSON.stringify(out));
