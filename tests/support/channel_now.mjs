/* Drives static/js/channel-now.js::channelNow() in jsdom over the shipped util.js and prints what
   it observed as JSON. Called by tests/test_channel_now_js.py, which owns every assertion - this
   file only reports. fetch is answered here by event id: 1 one answer, 2 two, 3 none, 4 a 404.
   followHref is recorded, since jsdom cannot navigate.

   argv: <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO] = process.argv;
const UTIL = fs.readFileSync(`${REPO}/static/js/util.js`, 'utf8');
const SCRIPT = fs.readFileSync(`${REPO}/static/js/channel-now.js`, 'utf8');
// The dialog's Preview buttons (dev/changelog/1645) come from live-preview.js, which both pages load.
const PREVIEW = fs.readFileSync(`${REPO}/static/js/live-preview.js`, 'utf8');

const PAGE = `<!doctype html><html><head><meta name="csrf-token" content="t"></head><body>
  <button id="one" data-act="channel-now" data-event="1">Channel it would use</button>
  <button id="two" data-act="channel-now" data-event="2">Channel it would use</button>
  <button id="none" data-act="channel-now" data-event="3">Channel it would use</button>
  <button id="gone" data-act="channel-now" data-event="4">Channel it would use</button>
  <button id="why" data-act="channel-now" data-event="5" aria-disabled="true"
    data-why="Your guide does not list this game yet, so there is no channel to show.">Channel it would use</button>
</body></html>`;

const acct = (id, name) => ({ id, name: `${name}<b>`, color: '#3a7', url: `/accounts/${id}` });
const ch = (id, name, a) => ({ id, name, url: `/channels/${id}`, account: a });
const ANSWERS = {
  1: { success: true, game: 'Gulls at Bears', one: 'game', same: true,
    record: { channel: ch(11, 'Local 1', acct(1, 'Acct One')), group: null, why: '', note: '' },
    play: { channel: ch(11, 'Local 1', acct(1, 'Acct One')), group: null, why: '', note: '', applies: true },
    footnote: '', go: '/channels/11',
    toast: 'If Gulls at Bears started now, ChannelBin would record it from this channel, and its sports channel on your IPTV output would play it from here too.' },
  2: { success: true, game: 'Gulls at Bears', one: 'game', same: false,
    record: { channel: ch(11, 'Local <1>', acct(1, 'Acct One')),
      group: { id: 5, name: 'Locals', url: '/groups/5' }, why: '', note: '' },
    play: { channel: ch(12, 'Local 2', acct(2, 'Acct Two')), group: null, why: '', note: '', applies: true },
    footnote: 'A recording of a group picks its channel as it starts.', go: null, toast: '' },
  3: { success: true, game: 'Gulls at Bears', one: 'game', same: false,
    record: { channel: null, group: null, why: 'This game is not in your guide yet.', note: '' },
    play: { channel: null, group: null, why: 'There is nothing for a sports channel to play either.', note: '', applies: true },
    footnote: '', go: null, toast: '' },
};

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
const dom = new JSDOM(PAGE, {
  runScripts: 'dangerously', pretendToBeVisual: true,
  url: 'http://localhost:5000/sports/1', virtualConsole: vc,
});
const w = dom.window;
const d = w.document;
// jsdom has no media playback; the player only needs these to exist.
w.HTMLMediaElement.prototype.pause = () => {};
w.HTMLMediaElement.prototype.load = () => {};
const calls = [];
w.fetch = async (url, opts = {}) => {
  calls.push({ url, method: opts.method || 'GET' });
  const id = Number(url.split('/')[4]);
  const body = ANSWERS[id] || { error: 'That event is no longer stored.' };
  return { ok: id in ANSWERS, status: id in ANSWERS ? 200 : 404, statusText: '', json: async () => body };
};
w.eval(UTIL);
w.eval(PREVIEW);
w.eval(SCRIPT);
const navs = [];
w.followHref = (e, url) => navs.push(url);

const toast = () => {
  const t = d.getElementById('app-toast');
  return t ? { text: t.textContent, cls: t.className } : null;
};

async function run(id, e = { button: 0 }) {
  calls.length = 0;
  navs.length = 0;
  d.querySelectorAll('.modal').forEach((m) => m.remove());
  const t = d.getElementById('app-toast');
  if (t) t.remove();
  w.sessionStorage.clear();
  await w.channelNow(d.getElementById(id), e);
  const ov = d.querySelector('.modal');
  return {
    calls: [...calls],
    navs: [...navs],
    pending: w.sessionStorage.length ? JSON.parse(w.sessionStorage.getItem(w.sessionStorage.key(0))) : null,
    toast: toast(),
    modal: ov ? {
      title: ov.querySelector('.modal-head h2').textContent,
      heads: Array.from(ov.querySelectorAll('.modal-body h3')).map((el) => el.textContent),
      links: Array.from(ov.querySelectorAll('.modal-body a')).map((a) => [a.textContent, a.getAttribute('href')]),
      text: ov.querySelector('.modal-body').textContent.replace(/\s+/g, ' ').trim(),
      html: ov.querySelector('.modal-body').innerHTML,
      buttons: Array.from(ov.querySelectorAll('.modal-foot .btn')).map((b) => b.textContent),
      previews: Array.from(ov.querySelectorAll('.modal-body [data-act="preview"]'))
        .map((b) => ({ channel: b.dataset.channel, name: b.dataset.name, text: b.textContent.trim() })),
    } : null,
  };
}

const out = { errors };
out.one = await run('one');
out.newTab = await run('one', { button: 0, ctrlKey: true });
out.two = await run('two');
out.none = await run('none');
out.gone = await run('gone');
out.why = await run('why');

// A Preview in the dialog closes it and opens the player on that channel, as the pages' click
// handlers do (live-preview.js::previewFrom).
await run('two');
calls.length = 0;
w.previewFrom(d.querySelectorAll('.modal-body [data-act="preview"]')[1]);
await new Promise((r) => setTimeout(r, 20));
out.previewed = {
  calls: [...calls],
  titles: Array.from(d.querySelectorAll('.modal .modal-head h2')).map((h) => h.textContent),
};
process.stdout.write(JSON.stringify(out));
