/* Drives static/js/play-move.js::movePlay() in jsdom and prints what it observed as JSON. Called
   by tests/test_play_move_js.py, which owns every assertion - this file only reports. The
   shipped util.js and play-move.js are evaluated as the pages' <script src> tags would; only
   fetch is stood in for, answering each move from a queue the run sets.

   argv: <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO] = process.argv;
const JS = ['util.js', 'play-move.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

const PAGE = `<!doctype html><html><head><meta name="csrf-token" content="t"></head><body>
  <button id="move" data-act="move-play" data-id="s/1" data-client="10.0.0.9"
          data-name="Game feeds" data-channel="Feed a">Move to another channel</button>
</body></html>`;

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
const dom = new JSDOM(PAGE, {
  runScripts: 'dangerously', pretendToBeVisual: true,
  url: 'http://localhost:5000/', virtualConsole: vc,
});
const w = dom.window;
const d = w.document;
const posts = [];
const answers = [];
w.fetch = async (url, opts = {}) => {
  posts.push(`${(opts.method || 'GET').toUpperCase()} ${url}`);
  const [status, body] = answers.shift();
  return { ok: status < 400, status, statusText: String(status), json: async () => body };
};
JS.forEach((src) => w.eval(src));

const toasts = () => Array.from(d.querySelectorAll('.toast')).map((t) => ({
  text: t.textContent.trim(), cls: t.className,
}));
const modal = () => d.querySelector('.modal');
const tick = () => new Promise((r) => setTimeout(r, 0));

async function run(answer, confirm) {
  if (answer) answers.push(answer);
  const before = posts.length;
  const p = w.movePlay(d.getElementById('move'));
  await tick();
  const ov = modal();
  const shown = ov ? {
    title: ov.querySelector('.modal-head h2').textContent,
    paras: Array.from(ov.querySelectorAll('.modal-body p')).map((el) => el.textContent),
    confirm: Array.from(ov.querySelectorAll('.modal-foot .btn')).pop().textContent,
  } : null;
  const btns = Array.from(ov.querySelectorAll('.modal-foot .btn'));
  (confirm ? btns[btns.length - 1] : btns[0]).click();
  const result = await p;
  await tick();
  const out = { shown, result, posts: posts.slice(before), toasts: toasts() };
  d.querySelectorAll('.toast').forEach((t) => t.remove());
  while (modal()) modal().remove();
  return out;
}

const out = {};
out.cancelled = await run(null, false);
out.moved = await run([200, { success: true, message: 'Moved to "Feed b".', warning: false }], true);
out.unsaved = await run([200, { success: true, message: 'Moved, not saved.', warning: true }], true);
out.refused = await run([409, { error: 'No other channel can play it right now.' }], true);
out.errors = errors;
process.stdout.write(JSON.stringify(out));
