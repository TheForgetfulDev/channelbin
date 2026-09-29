/* Drives the Recording Profile modal's filename designer step in jsdom and prints what it
   observed as JSON. Called by tests/test_profile_designer_js.py, which owns every assertion.

   What is real: util.js, dropdown.js, filename-designer.js and profile-modal.js are the
   shipped files, evaluated together the way their <script src> tags would be. Faked: the
   network - the designer's boot and preview requests are answered here, and every
   jsonFetch is recorded. jsdom computes no layout, so nothing here says where anything
   lands on screen; this is about which panel is showing, what each field holds and what
   was sent (dev/changelog/1161).

   argv: <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO] = process.argv;
const JS = ['util.js', 'dropdown.js', 'filename-designer.js', 'profile-modal.js']
  .map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));
const PAGE = '<!doctype html><html><head><meta name="csrf-token" content="t">'
  + '<meta name="display-tz" content="UTC"></head><body></body></html>';

const BOOT = {
  success: true, template: '{date} - {title} GLOBAL', remove: ['hd'], replace: [],
  tags: [{ name: 'live', color: '#f00', patterns: ['LIVE'] },
         { name: 'hd', color: '#0f0', patterns: ['HD'] }],
  variables: [{ name: '{title}', desc: 'Title' }], tag_variables: [], extension: 'mp4',
};

function boot() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
  const sent = [];
  const dom = new JSDOM(PAGE, {
    runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://localhost:5000/profiles',
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = (target, opts = {}) => {
        const url = new URL(String(target), 'http://localhost:5000/');
        const method = (opts.method || 'GET').toUpperCase();
        if (method !== 'GET') {
          sent.push({ path: url.pathname, method,
                      body: typeof opts.body === 'string' ? JSON.parse(opts.body) : null });
        }
        let payload = { success: true };
        if (url.pathname === '/api/filename-designer') payload = BOOT;
        if (url.pathname === '/api/template-preview') {
          payload = { success: true, name: url.searchParams.get('template'),
                      disk: url.searchParams.get('template'), changed: false, unknown: [],
                      also: [], subject: { title: 'Sample', channel: 'Chan' } };
        }
        if (url.pathname === '/api/channels/search') payload = { success: true, rows: [], total: 0 };
        if (url.pathname.startsWith('/api/profiles')) {
          payload = { success: true, profile: { id: 7 } };
        }
        return Promise.resolve({
          ok: true, status: 200, headers: { get: () => 'application/json' },
          json: () => Promise.resolve(payload),
          text: () => Promise.resolve(JSON.stringify(payload)),
        });
      };
    },
  });
  const w = dom.window;
  w.eval(JS.join('\n;\n'));
  w.TOASTS = [];
  w.eval(`showToast = function (m, o) { TOASTS.push({ msg: m, type: (o || {}).type || 'ok' }); };`);
  return { w, errors, sent };
}

const settle = async (w) => {
  for (let i = 0; i < 4; i += 1) await new Promise((r) => w.setTimeout(r, 0));
};
const $ = (w, sel) => w.document.querySelector(sel);
const esc = (w) => w.document.dispatchEvent(
  new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));

function view(w) {
  return {
    modals: w.document.querySelectorAll('.modal').length,
    title: ($(w, '.modal-head h2') || {}).textContent || null,
    designer: !!$(w, '#fd-tpl'),
    picker: !!$(w, '#fd-pksearch'),
    profile_form: !!$(w, '#pm-name'),
    primary: ($(w, '.modal-foot .btn-primary') || {}).textContent || null,
    xwide: !!$(w, '.modal-panel.modal-xwide.fd-modal'),
  };
}

const PROFILE = { id: 7, name: 'Sports', filename_template: '{title} P',
                  filename_tags_remove: ['live'], filename_tags_replace: [] };
const DEFAULTS = { filename_template: '{date} - {title} GLOBAL' };

function openEditor(w, profile) {
  return w.openRecordingProfileModal({ profile, defaults: DEFAULTS, posterSpec: null,
                                       onDone: () => { w.DONE = true; } });
}

const out = {};
const record = async (name, fn) => {
  try { out[name] = await fn(); } catch (e) { out[name] = { error: `${e.message}\n${e.stack}` }; }
};

/* ── Open the step, change the template, Use template, then save the profile ───────── */
await record('use_template_then_save', async () => {
  const { w, errors, sent } = boot();
  openEditor(w, PROFILE);
  const before = view(w);
  const nameBox = $(w, '#pm-name');
  nameBox.value = 'Sports edited';
  const summaryBefore = $(w, '[data-pm-tpl]').textContent.replace(/\s+/g, ' ').trim();
  $(w, '[data-pm-design]').click();
  await settle(w);
  const inStep = view(w);
  const startTpl = $(w, '#fd-tpl').value;
  const startRemove = w.FilenameDesigner.state.remove.slice();
  const tpl = $(w, '#fd-tpl');
  tpl.value = '{date} {title}';
  tpl.dispatchEvent(new w.Event('input', { bubbles: true }));
  $(w, '[data-fd-save]').click();
  await settle(w);
  const back = view(w);
  const after = {
    name: $(w, '#pm-name').value,
    hidden: $(w, '#pm-filename_template').value,
    summary: $(w, '[data-pm-tpl]').textContent.replace(/\s+/g, ' ').trim(),
    sent_before_profile_save: sent.map((s) => s.path),
  };
  $(w, '.modal-foot .btn-primary').click();
  await settle(w);
  return { errors, before, summaryBefore, inStep, startTpl, startRemove, back, after,
           save: sent.find((s) => s.path === '/api/profiles/7') || null };
});

/* ── Escape steps back one level at a time; Cancel leaves the field alone ──────────── */
await record('escape_ladder', async () => {
  const { w, errors } = boot();
  openEditor(w, PROFILE);
  $(w, '[data-pm-design]').click();
  await settle(w);
  $(w, '[data-fdsrc="guide"]').click();
  await settle(w);
  const picker = view(w);
  esc(w);
  const designer = view(w);
  const tpl = $(w, '#fd-tpl');
  tpl.value = 'abandoned';
  tpl.dispatchEvent(new w.Event('input', { bubbles: true }));
  esc(w);
  const profile = view(w);
  const hidden = $(w, '#pm-filename_template').value;
  // Reopen and Cancel, the button rather than the key.
  $(w, '[data-pm-design]').click();
  await settle(w);
  $(w, '[data-fd-cancel]').click();
  const afterCancel = view(w);
  esc(w);
  return { errors, picker, designer, profile, hidden, afterCancel, closed: view(w) };
});

/* ── A profile without a template starts from the global template and lists ───────── */
await record('blank_profile_starts_from_global', async () => {
  const { w, errors, sent } = boot();
  openEditor(w, null);
  const summary = $(w, '[data-pm-tpl]').textContent.replace(/\s+/g, ' ').trim();
  const clearHidden = $(w, '[data-pm-tpl-clear]').hidden;
  $(w, '[data-pm-design]').click();
  await settle(w);
  const startTpl = $(w, '#fd-tpl').value;
  const startRemove = w.FilenameDesigner.state.remove.slice();
  $(w, '[data-fd-save]').click();
  await settle(w);
  const hidden = $(w, '#pm-filename_template').value;
  const clearShown = !$(w, '[data-pm-tpl-clear]').hidden;
  $(w, '#pm-name').value = 'New one';
  $(w, '.modal-foot .btn-primary').click();
  await settle(w);
  return { errors, summary, clearHidden, startTpl, startRemove, hidden, clearShown,
           save: sent.find((s) => s.path === '/api/profiles') || null };
});

/* ── Use the global template clears the template and its lists ─────────────────────── */
await record('clear_to_global', async () => {
  const { w, errors, sent } = boot();
  openEditor(w, PROFILE);
  $(w, '[data-pm-tpl-clear]').click();
  const hidden = $(w, '#pm-filename_template').value;
  const summary = $(w, '[data-pm-tpl]').textContent.replace(/\s+/g, ' ').trim();
  $(w, '.modal-foot .btn-primary').click();
  await settle(w);
  return { errors, hidden, summary, save: sent.find((s) => s.path === '/api/profiles/7') || null };
});

/* ── The x while the step is showing closes everything and releases the designer ───── */
await record('close_from_step', async () => {
  const { w, errors } = boot();
  openEditor(w, PROFILE);
  $(w, '[data-pm-design]').click();
  await settle(w);
  $(w, '.modal-close').click();
  const closed = view(w);
  openEditor(w, PROFILE);
  $(w, '[data-pm-design]').click();
  await settle(w);
  return { errors, closed, reopened: view(w) };
});

/* ── Settings' own host is unchanged: Save posts to config, Escape in the picker steps
      back to the designer, and Escape in the designer closes it ─────────────────────── */
await record('settings_host', async () => {
  const { w, errors, sent } = boot();
  w.openFilenameDesigner({ onSave: () => { w.SAVED = true; } });
  await settle(w);
  const opened = view(w);
  $(w, '[data-fdsrc="guide"]').click();
  await settle(w);
  esc(w);
  const back = view(w);
  $(w, '[data-fd-save]').click();
  await settle(w);
  const afterSave = view(w);
  w.openFilenameDesigner({});
  await settle(w);
  esc(w);
  return { errors, opened, back, afterSave, saved: !!w.SAVED, closedByEscape: view(w),
           posts: sent.map((s) => s.path) };
});

process.stdout.write(JSON.stringify(out));
process.exit(0);
