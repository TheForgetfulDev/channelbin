/* Opens the running recording's Block accounts dialog (static/js/recording-switch.js
   ::openBlock) in jsdom and prints what its hint says, as JSON. Called by
   tests/test_recording_block_hint_js.py, which owns every assertion.

   The page body is the record modal's block picker exactly as a real /guide render drew it,
   because that rendered hint is the one copy the dialog reads (dev/changelog/1370). util.js
   and recording-switch.js are the shipped files; guide.js's picker loaders and the network
   are stand-ins, since only the hint is under test.

   argv: <fixture dir> <repo root>. The fixture dir holds picker.html. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const PICKER = fs.readFileSync(`${DIR}/picker.html`, 'utf8');
const JS = ['util.js', 'recording-switch.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

function boot() {
  const page = `<!doctype html><html><head><meta name="csrf-token" content="t"></head>
    <body>${PICKER}</body></html>`;
  const dom = new JSDOM(page, {
    runScripts: 'dangerously', pretendToBeVisual: true,
    url: 'http://192.168.1.100:5000/recordings/7', virtualConsole: vc,
  });
  const w = dom.window;
  w.fetch = () => Promise.resolve({
    ok: true, status: 200, headers: { get: () => 'application/json' },
    json: async () => ({ success: true, affects: false }),
  });
  w.loadBlockPicker = async () => {};
  w.blockPickerPayload = () => [];
  JS.forEach((src) => w.eval(src));
  return w;
}

const flush = () => new Promise((r) => setTimeout(r, 0));
const out = { errors };

const w = boot();
await w.recordingDialogs.block(7);
await flush();
const hint = w.document.querySelector('#rec-blk-blocks .form-hint');
out.dialogHint = hint ? hint.textContent : null;
out.pickerHint = w.document.querySelector('#active-rec-blocks .form-hint').textContent;

process.stdout.write(JSON.stringify(out));
