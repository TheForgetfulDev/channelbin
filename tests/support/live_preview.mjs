/* Drives static/js/live-preview.js (and util.js's player stage, re-encode offer and attachHls,
   with a stand-in hls.js) in jsdom and prints what it observed as JSON. Called by
   tests/test_live_preview_js.py, which owns every assertion - this file only reports.

   What it stages is the stream that made dev/changelog/1461: the browser's decoder refuses the
   copy and says so on the <video> element, while hls.js raises nothing. Network, timers and the
   media element's playback methods are the stand-ins jsdom lacks.

   argv: <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO] = process.argv;
const UTIL = fs.readFileSync(`${REPO}/static/js/util.js`, 'utf8');
const PREVIEW = fs.readFileSync(`${REPO}/static/js/live-preview.js`, 'utf8');

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
const dom = new JSDOM('<!doctype html><html><head><meta name="csrf-token" content="t"></head><body></body></html>', {
  runScripts: 'dangerously', pretendToBeVisual: true,
  url: 'http://192.168.1.100:5000/channels/9', virtualConsole: vc,
});
const w = dom.window;
const d = w.document;

const respond = (status, body) => Promise.resolve({
  ok: status >= 200 && status < 300, status, statusText: `HTTP ${status}`,
  headers: { get: () => null }, json: async () => body,
});
const posted = [];   // [url, parsed JSON body or null]
let reencodeAnswer = { success: true, available: true, reason: '' };
let feedStatus = [];
let nextSid = 1;
const sessions = {};
w.fetch = (url, opts = {}) => {
  if (opts.method && opts.method !== 'GET') {
    posted.push([url, typeof opts.body === 'string' ? JSON.parse(opts.body) : null]);
  }
  if (url === '/api/channels/9/preview') {
    const sid = `s${nextSid++}`;
    const reencode = !!(typeof opts.body === 'string' && JSON.parse(opts.body).reencode);
    sessions[sid] = reencode;
    return respond(200, {
      success: true, session_id: sid, state: 'STARTING', transcode_audio: false, reencode,
      status_url: `/api/preview/${sid}/status`, playlist_url: `/api/preview/${sid}/index.m3u8`,
      stop_url: `/api/preview/${sid}/stop`,
    });
  }
  if (url === '/api/preview/reencode') return respond(200, reencodeAnswer);
  // A feed preview (dev/changelog/1645): its status walks the rounds staged in feedStatus.
  if (url === '/api/feed/1000000001/preview') {
    return respond(200, { success: true, session_id: 'f1', source: 'feed', state: 'STARTING', round: 0,
      now_playing: 'the waiting screen', transcode_audio: true, reencode: false,
      status_url: '/api/preview/f1/status', playlist_url: '/api/preview/f1/index.m3u8?round=0',
      stop_url: '/api/preview/f1/stop' });
  }
  if (url === '/api/preview/f1/status') {
    const st = feedStatus.length > 1 ? feedStatus.shift() : feedStatus[0];
    return respond(200, { success: true, session_id: 'f1', source: 'feed', elapsed_seconds: 3,
      playlist_url: `/api/preview/f1/index.m3u8?round=${st.round}`, ...st });
  }
  const m = url.match(/^\/api\/preview\/(s\d+)\/status$/);
  if (m) return respond(200, { success: true, session_id: m[1], state: 'READY', elapsed_seconds: 2 });
  if (url.endsWith('/stop')) return respond(200, { success: true, stopped: true });
  return respond(404, { error: `unexpected ${url}` });
};

let clock = 0;
const timeouts = new Map();
let nextId = 1;
w.setTimeout = (fn, ms = 0) => { const id = nextId++; timeouts.set(id, { fn, due: clock + ms }); return id; };
w.clearTimeout = (id) => { timeouts.delete(id); };
const tick = (ms) => {
  clock += ms;
  for (;;) {
    const due = [...timeouts.entries()].filter(([, t]) => t.due <= clock).sort((a, b) => a[1].due - b[1].due);
    if (!due.length) return;
    const [id, t] = due[0];
    timeouts.delete(id);
    t.fn();
  }
};
const flush = () => new Promise((r) => setImmediate(r));
const settle = async () => { for (let i = 0; i < 8; i++) await flush(); };

w.MediaError = { MEDIA_ERR_ABORTED: 1, MEDIA_ERR_NETWORK: 2, MEDIA_ERR_DECODE: 3, MEDIA_ERR_SRC_NOT_SUPPORTED: 4 };
w.HTMLMediaElement.prototype.play = () => Promise.resolve();
w.HTMLMediaElement.prototype.pause = () => {};
w.HTMLMediaElement.prototype.load = () => {};
w.HTMLMediaElement.prototype.canPlayType = () => '';

const hlsMade = [];
class FakeHls {
  static isSupported() { return true; }
  constructor(config) { this.config = config; this.handlers = {}; this.destroyed = false; this.recovered = 0; hlsMade.push(this); }
  on(evt, fn) { (this.handlers[evt] = this.handlers[evt] || []).push(fn); }
  emit(evt, data) { (this.handlers[evt] || []).forEach((fn) => fn(evt, data)); }
  loadSource(url) { this.url = url; }
  attachMedia(v) { this.media = v; }
  destroy() { this.destroyed = true; }
  recoverMediaError() { this.recovered += 1; }
}
FakeHls.Events = { MANIFEST_PARSED: 'hlsManifestParsed', ERROR: 'hlsError', INIT_PTS_FOUND: 'hlsInitPtsFound' };
FakeHls.ErrorTypes = { MEDIA_ERROR: 'mediaError', NETWORK_ERROR: 'networkError', OTHER_ERROR: 'otherError' };
w.Hls = FakeHls;

const s1 = d.createElement('script'); s1.textContent = UTIL; d.head.appendChild(s1);
const s2 = d.createElement('script'); s2.textContent = PREVIEW; d.head.appendChild(s2);

const view = () => {
  const m = d.querySelector('.modal');
  if (!m) return null;
  const st = m.querySelector('.player-status');
  const box = m.querySelector('.player-offer');
  return {
    status: st.textContent, kind: st.dataset.kind,
    offer: box ? box.textContent.replace(/\s+/g, ' ').trim() : null,
    button: box && box.querySelector('button') ? box.querySelector('button').textContent : null,
  };
};
const setError = (v, err) => Object.defineProperty(v, 'error', { value: err, writable: true, configurable: true });
const DECODE = { code: 3, message: 'PipelineStatus::PIPELINE_ERROR_DECODE: Failed to send video packet for decoding' };
const out = { errors };

// The copy: READY, attached, then the element errors while hls.js says nothing.
w.openLivePreview({ channelId: 9, channelName: 'PT Sport' });
await settle();
tick(1000); await settle();                // the poll: READY -> attach
const v = d.querySelector('.modal video');
const first = hlsMade[hlsMade.length - 1];
setError(v, DECODE);
v.dispatchEvent(new w.Event('error'));
await settle();
out.firstError = { ...view(), recovered: first.recovered };
v.dispatchEvent(new w.Event('error'));
await settle();
out.failed = { ...view(), recovered: first.recovered, destroyed: first.destroyed,
  stops: posted.filter(([u]) => u.endsWith('/stop')).map(([u]) => u) };

// Try re-encoded: a new session asked for the re-encode, playing says so.
posted.length = 0;
const offerBtn = d.querySelector('.modal .player-offer button');
if (offerBtn) offerBtn.click();   // absent is what the test reports, not a crash
await settle();
out.clicked = { ...view(), posted: posted.slice() };
setError(v, null);
tick(1000); await settle();
const second = hlsMade[hlsMade.length - 1];   // the first again when nothing was re-started
v.dispatchEvent(new w.Event('playing'));
out.reencodePlaying = view();

// The re-encode fails too: said as the re-encode, nothing more offered.
second.emit('hlsError', { fatal: true, type: 'mediaError', details: 'bufferAppendError', error: { message: 'append failed' } });
second.emit('hlsError', { fatal: true, type: 'mediaError', details: 'bufferAppendError', error: { message: 'append failed' } });
await settle();
out.reencodeFailed = { ...view(), recovered: second.recovered };
d.querySelector('.modal .modal-close').click();
await settle();

// No GPU: the decode failure is followed by the server's reason, not a button.
reencodeAnswer = { success: true, available: false, reason: 'ChannelBin re-encodes only on a GPU, and there is no GPU device at /dev/dri/renderD128 (recording.post_process.vaapi_device).' };
w.openLivePreview({ channelId: 9, channelName: 'PT Sport' });
await settle();
tick(1000); await settle();
const third = hlsMade[hlsMade.length - 1];
third.emit('hlsError', { fatal: true, type: 'mediaError', details: 'bufferAddCodecError', error: { message: "Failed to execute 'addSourceBuffer' on 'MediaSource': The type provided ('video/mp4;codecs=hvc1.1.6.L93.B0') is unsupported." } });
third.emit('hlsError', { fatal: true, type: 'mediaError', details: 'bufferAddCodecError', error: { message: "Failed to execute 'addSourceBuffer' on 'MediaSource': The type provided ('video/mp4;codecs=hvc1.1.6.L93.B0') is unsupported." } });
await settle();
out.noGpu = view();
d.querySelector('.modal .modal-close').click();
await settle();

// A feed preview: round 1 on the waiting screen, then the output moves to the game - a mark ends
// round 1 (STARTING) and round 2 is READY on the game's channel. The player lets go of round 1 and
// attaches to round 2 with a decoder of its own.
feedStatus = [
  { state: 'STARTING', round: 1, now_playing: 'the waiting screen' },
  { state: 'READY', round: 1, now_playing: 'the waiting screen' },
  { state: 'STARTING', round: 1, now_playing: 'the waiting screen' },
  { state: 'READY', round: 2, now_playing: '"Local game"' },
];
const madeBefore = hlsMade.length;
w.openLivePreview({ feedId: 1000000001, channelName: 'Ironvale Bears' });
await settle();                            // STARTING round 1: the first connect
out.feedConnecting = view();
tick(1000); await settle();                // READY round 1 -> attach
const r1 = hlsMade[hlsMade.length - 1];
d.querySelector('.modal video').dispatchEvent(new w.Event('playing'));
out.feedRound1 = { ...view(), url: r1 && r1.url, made: hlsMade.length - madeBefore,
  posted: posted.filter(([u]) => u.startsWith('/api/feed/')).map(([u]) => u) };
tick(3000); await settle();                // STARTING: round 1 is let go
out.feedSwitching = { ...view(), r1Destroyed: r1 && r1.destroyed, made: hlsMade.length - madeBefore };
tick(1000); await settle();                // READY round 2 -> attach again
const r2 = hlsMade[hlsMade.length - 1];
d.querySelector('.modal video').dispatchEvent(new w.Event('playing'));
out.feedRound2 = { ...view(), url: r2 && r2.url, made: hlsMade.length - madeBefore };
d.querySelector('.modal .modal-close').click();

process.stdout.write(JSON.stringify(out));
w.close();
