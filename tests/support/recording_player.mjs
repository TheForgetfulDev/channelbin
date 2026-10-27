/* Drives static/js/recording-player.js (and util.js's copyText / buildPlayerStage /
   attachHls, with a stand-in hls.js) in jsdom
   and prints what it observed as JSON. Called by tests/test_recording_player_js.py, which owns
   every assertion - this file only reports. The shipped scripts are evaluated the way the
   recording page's <script src> tags would be, not stubbed.

   The page is served from a bare LAN IP over plain http, which is not a secure context:
   navigator.clipboard is absent there, exactly as jsdom leaves it. Network, timers, the media
   element's playback methods and execCommand are the stand-ins jsdom lacks.

   argv: <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO] = process.argv;
const UTIL = fs.readFileSync(`${REPO}/static/js/util.js`, 'utf8');
const PLAYER = fs.readFileSync(`${REPO}/static/js/recording-player.js`, 'utf8');

const PAGE = `<!doctype html><html><head><meta name="csrf-token" content="t"></head><body>
  <div class="detail-head"><div><h1>Race day</h1></div></div>
</body></html>`;

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
const dom = new JSDOM(PAGE, {
  runScripts: 'dangerously', pretendToBeVisual: true,
  url: 'http://192.168.1.100:5000/recordings/7', virtualConsole: vc,
});
const w = dom.window;
const d = w.document;

// ── stand-ins ──────────────────────────────────────────────────────────────
let answer = null;   // what the next /file/check fetch answers: a function returning a promise
const respond = (status, body) => Promise.resolve({
  ok: status >= 200 && status < 300, status, statusText: `HTTP ${status}`,
  headers: { get: () => null }, json: async () => body,
});
const fetched = [];
const posted = [];   // [url, parsed JSON body] for every non-GET request
w.fetch = (url, opts = {}) => {
  fetched.push(url);
  if (opts.method && opts.method !== 'GET') {
    posted.push([url, typeof opts.body === 'string' ? JSON.parse(opts.body) : null]);
  }
  return answer(url, opts);
};

// One fake clock for intervals and timeouts alike, so the player's waits (the storage
// counter, the seek settle, the status poll, the pause hold) run only when tick() says.
let clock = 0;
w.Date.now = () => clock;
const intervals = new Map();
const timeouts = new Map();
let nextId = 1;
w.setInterval = (fn) => { const id = nextId++; intervals.set(id, fn); return id; };
w.clearInterval = (id) => { intervals.delete(id); };
w.setTimeout = (fn, ms = 0) => { const id = nextId++; timeouts.set(id, { fn, due: clock + ms }); return id; };
w.clearTimeout = (id) => { timeouts.delete(id); };
const runDue = () => {
  for (;;) {
    const due = [...timeouts.entries()].filter(([, t]) => t.due <= clock).sort((a, b) => a[1].due - b[1].due);
    if (!due.length) return;
    const [id, t] = due[0];
    timeouts.delete(id);
    t.fn();
  }
};
const tick = (ms) => { clock += ms; [...intervals.values()].forEach((fn) => fn()); runDue(); };

w.MediaError = { MEDIA_ERR_ABORTED: 1, MEDIA_ERR_NETWORK: 2, MEDIA_ERR_DECODE: 3, MEDIA_ERR_SRC_NOT_SUPPORTED: 4 };
w.HTMLMediaElement.prototype.play = () => Promise.resolve();
w.HTMLMediaElement.prototype.pause = () => {};
w.HTMLMediaElement.prototype.load = () => {};

let execOk = true;
const copied = [];
d.execCommand = (cmd) => {
  const ta = d.querySelector('textarea');
  copied.push({ cmd, value: ta ? ta.value : null });
  return execOk;
};

const anchorClicks = [];
w.HTMLAnchorElement.prototype.click = function click() {
  anchorClicks.push({ href: this.getAttribute('href'), download: this.download });
};

w.eval(UTIL);
w.eval(PLAYER);

const flush = () => new Promise((r) => setTimeout(r, 0));
const modal = () => d.querySelector('.modal');
const view = () => {
  const m = modal();
  if (!m) return null;
  const st = m.querySelector('.player-status');
  const v = m.querySelector('video');
  const dl = m.querySelector('.modal-foot .btn');
  return {
    title: m.querySelector('.modal-head h2').textContent,
    panelClass: m.querySelector('.modal-panel').className,
    status: st.textContent,
    kind: st.dataset.kind,
    src: v.getAttribute('src'),
    downloadLabel: dl ? dl.textContent : null,
    downloadDisabled: dl ? dl.disabled : null,
  };
};
const closeModal = () => { const m = modal(); if (m) m.querySelector('.modal-close').click(); };

const OK = (over = {}) => ({
  success: true, name: 'Race day.mp4', size: 3221225472, opened_seconds: 0.1,
  native: true, reason: null, caveat: null, video_codec: 'h264', audio_codec: 'aac',
  media_url: '/recordings/7/file', download_url: '/recordings/7/file?download=1', ...over,
});

const out = { errors };
out.secureContext = { clipboard: w.navigator.clipboard === undefined ? 'absent' : 'present' };

// ── copyText on a non-secure origin ─────────────────────────────────────────
execOk = true;
out.copyOk = await w.copyText('/dvr/Race day.mp4').then(() => 'resolved', (e) => `rejected: ${e.message}`);
out.copiedValue = copied.length ? copied[copied.length - 1].value : null;
out.textareaLeftBehind = d.querySelectorAll('textarea').length;
execOk = false;
out.copyRefused = await w.copyText('x').then(() => 'resolved', (e) => `rejected: ${e.message}`);
execOk = true;

// ── Play: a slow storage, then a playable file ─────────────────────────────
let release;
answer = () => new Promise((r) => { release = r; });
clock = 0;
w.recordingDialogs.play(7);
out.slow = { start: view() };
tick(1000);
out.slow.at1s = view();
tick(8000);
out.slow.at9s = view();
tick(52000);
out.slow.at61s = view();
release(await respond(200, OK({ opened_seconds: 61.4 })));
await flush(); await flush();
out.slow.answered = view();
out.slow.fetched = fetched[fetched.length - 1];
out.slow.intervalsLeft = intervals.size;
d.querySelector('.modal video').dispatchEvent(new w.Event('playing'));
out.slow.playing = view();
d.querySelector('.modal .modal-foot .btn').click();
out.slow.footerDownload = anchorClicks[anchorClicks.length - 1];
out.slow.stillOpen = !!modal();
closeModal();
out.slow.afterClose = { modals: d.querySelectorAll('.modal').length };

// ── Play: the file is gone ─────────────────────────────────────────────────
answer = () => respond(404, { error: 'Could not open /dvr/Race day.mp4: No such file or directory (ENOENT). Its folder /dvr is readable, and the file is not in it.' });
w.recordingDialogs.play(7);
await flush(); await flush();
out.missing = view();
closeModal();

// ── Play: the server could not be reached at all ───────────────────────────
answer = () => Promise.reject(new TypeError('Failed to fetch'));
w.recordingDialogs.play(7);
await flush(); await flush();
out.unreachable = view();
closeModal();

// ── Play: an unconverted .ts ───────────────────────────────────────────────
answer = () => respond(200, OK({
  name: 'Race day.ts', native: false, video_codec: 'h264',
  reason: 'This recording was kept as an MPEG-TS (.ts) file, which a browser cannot play by itself. Download it and play it in VLC or another media player.',
}));
w.recordingDialogs.play(7);
await flush(); await flush();
out.notNative = view();
closeModal();

// ── Play: the browser cannot decode it ─────────────────────────────────────
answer = () => respond(200, OK({ video_codec: 'hevc', caveat: 'The video is HEVC (H.265). Safari plays it, and Chrome or Edge do on hardware with an HEVC decoder; Firefox does not.' }));
w.recordingDialogs.play(7);
await flush(); await flush();
out.hevcLoading = view();
const v = d.querySelector('.modal video');
Object.defineProperty(v, 'error', { value: { code: 4, message: 'DEMUXER_ERROR_NO_SUPPORTED_STREAMS' } });
v.dispatchEvent(new w.Event('error'));
out.hevcError = view();
closeModal();

// ── Play: closed before the storage answered ───────────────────────────────
answer = () => new Promise((r) => { release = r; });
w.recordingDialogs.play(7);
closeModal();
release(await respond(200, OK()));
await flush(); await flush();
out.closedEarly = { modals: d.querySelectorAll('.modal').length, intervalsLeft: intervals.size };

// ── Download from the kebab ────────────────────────────────────────────────
const clicksBefore = anchorClicks.length;
answer = () => respond(200, OK());
w.recordingDialogs.download(7);
out.kebabWaitingToast = d.getElementById('app-toast').textContent;
await flush(); await flush();
out.kebabDownload = { click: anchorClicks.slice(clicksBefore), toast: d.getElementById('app-toast').textContent };
answer = () => respond(503, { error: 'Could not open /dvr/x.mp4: No such file or directory (ENOENT). Its folder /dvr could not be read either: Stale file handle (ESTALE).' });
w.recordingDialogs.download(7);
await flush(); await flush();
const toast = d.getElementById('app-toast');
out.kebabStale = { toast: toast.textContent, cls: toast.className };

// ── Play: a .ts through the preview pipeline (dev/changelog/1314) ─────────────
const hlsMade = [];
class FakeHls {
  static isSupported() { return true; }
  constructor(config) { this.config = config; this.handlers = {}; this.destroyed = false; hlsMade.push(this); }
  on(evt, fn) { (this.handlers[evt] = this.handlers[evt] || []).push(fn); }
  emit(evt, data) { (this.handlers[evt] || []).forEach((fn) => fn(evt, data)); }
  loadSource(url) { this.url = url; }
  attachMedia(v) { this.media = v; }
  destroy() { this.destroyed = true; }
  recoverMediaError() { this.recovered = (this.recovered || 0) + 1; }
}
FakeHls.Events = { MANIFEST_PARSED: 'hlsManifestParsed', ERROR: 'hlsError', INIT_PTS_FOUND: 'hlsInitPtsFound' };
FakeHls.ErrorTypes = { MEDIA_ERROR: 'mediaError', NETWORK_ERROR: 'networkError', OTHER_ERROR: 'otherError' };
w.Hls = FakeHls;

let nextSid = 1;
let statusFor = () => ({ state: 'READY' });   // what /api/preview/<sid>/status answers
const session = (sid) => ({
  success: true, session_id: sid, source: 'file', state: 'STARTING', transcode_audio: false,
  status_url: `/api/preview/${sid}/status`, playlist_url: `/api/preview/${sid}/index.m3u8`,
  stop_url: `/api/preview/${sid}/stop`,
});
const PIPE = OK({
  name: 'Race day.ts', native: false, pipeline: true, duration: 7200,
  playback_url: '/api/recordings/7/playback',
  reason: 'This recording was kept as an MPEG-TS (.ts) file, which a browser cannot play by itself.',
});
answer = (url) => {
  if (url.endsWith('/file/check')) return respond(200, PIPE);
  if (url === '/api/recordings/7/playback') return respond(200, session(`s${nextSid++}`));
  const m = url.match(/^\/api\/preview\/(s\d+)\/status$/);
  if (m) return respond(200, { success: true, session_id: m[1], ...statusFor(m[1]) });
  if (url.endsWith('/stop')) return respond(200, { success: true, stopped: true });
  return respond(404, { error: `unexpected ${url}` });
};
const settle = async () => { for (let i = 0; i < 6; i++) await flush(); };
const pv = () => {
  const m = modal();
  const base = view();
  return {
    ...base,
    pos: m.querySelector('.player-pos').textContent,
    pauseLabel: m.querySelector('.player-pause').textContent,
    barMax: m.querySelector('.player-seek-bar').max,
    barHidden: m.querySelector('.player-seek-bar').hidden,
    skips: [...m.querySelectorAll('[data-skip]')].map((b) => b.textContent),
  };
};
const setMedia = (v, props) => Object.entries(props).forEach(([k, value]) => (
  Object.defineProperty(v, k, { value, writable: true, configurable: true })));

out.pipe = {};
posted.length = 0;
w.recordingDialogs.play(7);
await settle();
const pvid = d.querySelector('.modal video');
out.pipe.started = { ...pv(), posted: posted.slice() };
tick(500); await settle();                                 // poll: READY -> attach
out.pipe.hls = { count: hlsMade.length, config: hlsMade[0] && hlsMade[0].config, url: hlsMade[0] && hlsMade[0].url };
hlsMade[0].emit('hlsInitPtsFound', { initPTS: 2.6 * 90000, timescale: 90000, frag: { start: 0 } });
setMedia(pvid, { currentTime: 10, paused: false });
pvid.dispatchEvent(new w.Event('timeupdate'));
pvid.dispatchEvent(new w.Event('playing'));
out.pipe.playing = pv();

// Three quick +10 min clicks are one seek, from where it really is (12.6 s in).
posted.length = 0;
const plus10 = [...d.querySelectorAll('.modal [data-skip]')].find((b) => b.dataset.skip === '600');
plus10.click(); plus10.click(); plus10.click();
out.pipe.seekPending = { ...pv(), posted: posted.length };
tick(400); await settle();
out.pipe.seeked = { ...pv(), posted: posted.slice(), oldDestroyed: hlsMade[0].destroyed };

// The landing the player reports is trusted near the request, and ignored far from it.
tick(500); await settle();
hlsMade[1].emit('hlsInitPtsFound', { initPTS: 5000 * 90000, timescale: 90000, frag: { start: 0 } });
setMedia(pvid, { currentTime: 0 });
pvid.dispatchEvent(new w.Event('timeupdate'));
out.pipe.untrustedOrigin = pv().pos;

// A pause shorter than the hold resumes in place; a longer one lets the session go.
posted.length = 0;
setMedia(pvid, { paused: true, currentTime: 5 });
pvid.dispatchEvent(new w.Event('pause'));
tick(29000); await settle();
out.pipe.shortPause = { posted: posted.length, destroyed: hlsMade[1].destroyed };
tick(1000); await settle();
out.pipe.held = { ...pv(), posted: posted.slice(), destroyed: hlsMade[1].destroyed };
posted.length = 0;
d.querySelector('.modal .player-pause').click();
await settle();
out.pipe.resumed = { ...pv(), posted: posted.slice() };

// The bar seeks on release; dragging only moves the label.
tick(500); await settle();
posted.length = 0;
const bar = d.querySelector('.modal .player-seek-bar');
bar.value = '3600';
bar.dispatchEvent(new w.Event('input'));
out.pipe.dragging = { pos: pv().pos, posted: posted.length };
bar.dispatchEvent(new w.Event('change'));
tick(400); await settle();
out.pipe.barSeek = posted.slice();

// The server stops the session for a reason: the player says it, in the server's words.
tick(500); await settle();
statusFor = () => ({ state: 'STOPPED', reason: 'read_failed', reason_text: "ffmpeg stopped reading the recording's file.", detail: 'ffmpeg exited with code 1: Input/output error' });
tick(3000); await settle();
out.pipe.readFailed = pv();
closeModal();

// The end of the file: the server is done first, the player plays out its buffer.
statusFor = () => ({ state: 'READY' });
hlsMade.length = 0;
w.recordingDialogs.play(7);
await settle();
tick(500); await settle();
const evid = d.querySelector('.modal video');
setMedia(evid, { paused: false, currentTime: 100 });
statusFor = () => ({ state: 'STOPPED', reason: 'file_end', reason_text: 'Reached the end of the recording.', detail: '' });
tick(3000); await settle();
out.pipe.serverDone = { ...pv(), destroyed: hlsMade[0].destroyed };
setMedia(evid, { ended: true });
evid.dispatchEvent(new w.Event('ended'));
out.pipe.atEnd = pv();
setMedia(evid, { ended: false });   // a detached video is no longer at its end
statusFor = () => ({ state: 'READY' });
posted.length = 0;
d.querySelector('.modal .player-pause').click();
await settle();
out.pipe.replay = posted.slice();
closeModal();

// Closing stops the live session and leaves no wait behind.
statusFor = () => ({ state: 'READY' });
w.recordingDialogs.play(7);
await settle();
tick(500); await settle();
posted.length = 0;
closeModal();
await settle();
out.pipe.closed = { posted: posted.slice(), modals: d.querySelectorAll('.modal').length, timeoutsLeft: [...timeouts.values()].filter((t) => t.due < clock + 60000).length };

// No duration measured: the bar hides, the skips still work.
answer = ((prev) => (url) => (url.endsWith('/file/check') ? respond(200, { ...PIPE, duration: null }) : prev(url)))(answer);
w.recordingDialogs.play(7);
await settle();
out.pipe.noDuration = pv();
closeModal();
delete w.Hls;

// ── Play: a recording still capturing (dev/changelog/1316) ─────────────────────
// Segment 0 is 100 s and finished; segment 1 is being written. The capture's own fields ride
// on each start response: `capture` places the session, `captured_seconds` the live edge.
w.Hls = FakeHls;
hlsMade.length = 0;
const CAP = {
  success: true, name: 'Race day', opened_seconds: 0, native: false, pipeline: false,
  capture: true, live: true, download: false, video_codec: 'h264', audio_codec: 'aac',
  reason: null, caveat: null, playback_url: '/api/recordings/7/playback',
};
let startAnswer = null;                       // (body) -> [status, json] for the next start
let lastSid = null;                           // the session the player is on now
// The server stops the session the player is on, and only that one: the next session's
// status is READY.
const stopCurrent = (payload) => {
  const sid = lastSid;
  statusFor = (asked) => (asked === sid ? { state: 'STOPPED', detail: '', ...payload } : { state: 'READY' });
};
const capSession = (sid, over = {}) => ({
  ...session(sid), following: true, captured_seconds: 160, capturing: true, capture_note: '',
  moved_because: '', capture: { segment_number: 1, base: 100, span_end: null, live: true }, ...over,
});
answer = (url, opts = {}) => {
  if (url.endsWith('/file/check')) return respond(200, CAP);
  if (url === '/api/recordings/7/playback') {
    const [code, body] = startAnswer(JSON.parse(opts.body));
    if (body.session_id) lastSid = body.session_id;
    return respond(code, body);
  }
  const m = url.match(/^\/api\/preview\/(s\d+)\/status$/);
  if (m) return respond(200, { success: true, session_id: m[1], ...statusFor(m[1]) });
  if (url.endsWith('/stop')) return respond(200, { success: true, stopped: true });
  return respond(404, { error: `unexpected ${url}` });
};
statusFor = () => ({ state: 'READY' });
startAnswer = () => [200, capSession(`s${nextSid++}`)];
posted.length = 0;
clock = 0;
w.recordingDialogs.play(7);
await settle();
const cv = d.querySelector('.modal video');
const capView = () => {
  const live = d.querySelector('.modal .player-live');
  return { ...pv(), liveHidden: live.hidden, livePrimary: live.classList.contains('btn-primary') };
};
out.cap = { started: { ...capView(), posted: posted.slice() } };
tick(500); await settle();                     // READY -> attach
// The follower landed 4.6 s into segment 1, which starts 100 s into the recording.
hlsMade[0].emit('hlsInitPtsFound', { initPTS: 4.6 * 90000, timescale: 90000 });
setMedia(cv, { currentTime: 2, paused: false });
cv.dispatchEvent(new w.Event('timeupdate'));
cv.dispatchEvent(new w.Event('playing'));
out.cap.live = capView();

// The recorder moves to segment 2: the player starts live again, naming the segment it left,
// and says why in the recording's own words.
posted.length = 0;
startAnswer = () => [200, capSession(`s${nextSid++}`, {
  moved_because: 'File stalled at 6316621824 bytes for 10.0s. Segment 1 ended: STALLED.',
  capture: { segment_number: 2, base: 165, span_end: null, live: true }, captured_seconds: 170,
})];
stopCurrent({ reason: 'segment_changed', reason_text: 'The recording moved on to a new segment file.' });
tick(3000); await settle();
out.cap.moved = { posted: posted.slice() };
tick(500); await settle();
cv.dispatchEvent(new w.Event('playing'));
out.cap.movedPlaying = capView();

// Skipping back a minute leaves live: a position, no `live`, and the Live button stops
// being the current one.
posted.length = 0;
startAnswer = (b) => [200, capSession(`s${nextSid++}`, {
  following: false, capture: { segment_number: 1, base: 100, span_end: 160, live: false },
  offset: b.offset - 100,
})];
setMedia(cv, { currentTime: 0 });
const back60 = [...d.querySelectorAll('.modal [data-skip]')].find((b) => b.dataset.skip === '-60');
back60.click();
tick(400); await settle();
out.cap.back = { posted: posted.slice() };
tick(500); await settle();
out.cap.behind = capView();

// That finished segment plays to its end; the player carries on where the next one starts.
posted.length = 0;
startAnswer = () => [200, capSession(`s${nextSid++}`)];
stopCurrent({ reason: 'file_end', reason_text: 'Reached the end of the recording.' });
tick(3000); await settle();
setMedia(cv, { ended: true });
cv.dispatchEvent(new w.Event('ended'));
setMedia(cv, { ended: false });
await settle();
out.cap.chained = { posted: posted.slice() };

// The Live button goes back to the live edge.
tick(500); await settle();
posted.length = 0;
d.querySelector('.modal .player-live').click();
await settle();
out.cap.liveClick = posted.slice();

// Between segments the server answers 409: the player says so and asks again.
tick(500); await settle();
posted.length = 0;
let refusals = 1;
startAnswer = () => (refusals-- > 0
  ? [409, { error: 'The capture is starting segment 3, which has not written anything yet.' }]
  : [200, capSession(`s${nextSid++}`)]);
stopCurrent({ reason: 'segment_changed', reason_text: 'The recording moved on to a new segment file.' });
tick(3000); await settle();
out.cap.betweenSegments = { ...capView(), posted: posted.length };
tick(2000); await settle();
out.cap.retried = { posted: posted.length };

// The capture stops for good and the join starts: the player says so in the server's words,
// and does not keep asking.
tick(500); await settle();
posted.length = 0;
startAnswer = () => [409, { error: 'Its final file is being made from the captured segments now.' }];
stopCurrent({ reason: 'capture_stopped', reason_text: 'The recording stopped capturing.' });
tick(3000); await settle();
tick(10000); await settle();
out.cap.stopped = { ...capView(), posted: posted.length };
closeModal();

// Paused: the server's note says what is being played.
CAP.live = false;
startAnswer = () => [200, capSession(`s${nextSid++}`, {
  following: false, capturing: false, captured_seconds: 130,
  capture_note: 'The recording is paused, so this plays what was captured before the pause.',
  capture: { segment_number: 0, base: 0, span_end: 100, live: false },
})];
statusFor = () => ({ state: 'READY' });
posted.length = 0;
w.recordingDialogs.play(7);
await settle();
tick(500); await settle();
d.querySelector('.modal video').dispatchEvent(new w.Event('playing'));
out.cap.paused = { ...capView(), posted: posted.slice() };
closeModal();
delete w.Hls;

out.dialogsRegistered = Object.keys(w.recordingDialogs).sort();

process.stdout.write(JSON.stringify(out));
// The last toast arms a 10 s hide timer; closing the window clears it instead of holding
// node open until it fires.
w.close();
