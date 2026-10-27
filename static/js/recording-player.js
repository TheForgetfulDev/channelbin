/* The recording page's Play and Download (dev/docs/DESIGN-playback.md, dev/changelog/1312, 1314).

   The page offers both from the database row alone. The file is first touched here, on the
   click, through GET /recordings/<id>/file/check: that wakes storage that has spun down - ten
   seconds or more is normal - and says plainly when the file is no longer where ChannelBin
   left it. Only then does the player get a URL, so a missing file is named instead of showing
   as a black box.

   Play takes one of two routes, decided on the server (app/playback.py::offer):
   - a converted .mp4 plays in the browser's own player, which seeks with HTTP Range requests;
   - a .ts or .mkv plays through the preview pipeline (app/preview.py file sessions): ffmpeg
     repackages the file into a rolling HLS window from a chosen position, and hls.js plays it.
     That window is not the whole file, so the modal's own controls do the seeking, and a seek
     is a new session at the new position. Nothing is re-encoded but audio a browser lacks.

   The page's delegated click handler hands `play` and `download` here through
   window.recordingDialogs. */

(() => {
  // How long the wait stays a plain "Opening the file" before it explains itself, and when
  // it adds that the storage may be offline. The request itself is never cut off: a disk that
  // answers at 70 s still plays.
  const EXPLAIN_AFTER_S = 3;
  const OFFLINE_HINT_AFTER_S = 60;
  const WOKE_NOTE_AFTER_S = 3;
  // A pipeline session paused longer than this is stopped, so a paused tab stops reading the
  // share, and Play restarts it where it was. Must stay under app/preview.py's
  // FILE_WINDOW_SECONDS (60): a shorter pause resumes from segments still on disk.
  const PAUSE_HOLD_S = 30;
  // Skip clicks within this long of each other add up into one seek, not one restart each.
  const SEEK_SETTLE_MS = 400;
  // Where the player says it landed is trusted only this close to where it was asked to
  // start; anything further is a timestamp the mapping does not understand, and the
  // requested position is shown instead.
  const ORIGIN_TRUST_S = 30;
  const SKIPS = [-600, -60, -10, 10, 60, 600];
  // A capture between segments, or not yet started, is asked again this often, this many
  // times, before the player gives up and shows the server's answer.
  const CAPTURE_RETRY_MS = 2000;
  const CAPTURE_RETRIES = 15;

  const HLS_SRC = (document.currentScript && document.currentScript.dataset.hlsJs) || '';

  const recName = () => {
    const h = document.querySelector('.detail-head h1');
    return h ? h.textContent.trim() : 'Recording';
  };

  const waitingText = (secs) => {
    if (secs < EXPLAIN_AFTER_S) return 'Opening the file…';
    let text = `Waiting for the storage to answer… (${secs}s)\n` +
      'A drive that has spun down to save power can take ten seconds or more to wake.';
    if (secs >= OFFLINE_HINT_AFTER_S) {
      text += '\nStill no answer after a minute - the storage may be offline. Close this to stop waiting.';
    }
    return text;
  };

  // A fetch that never reached the server rejects with a bare TypeError ("Failed to fetch");
  // say what that means here rather than repeating the browser's words.
  const errorText = (e) => (e && e.status ? e.message
    : 'Lost contact with ChannelBin before the file could be opened.');

  const skipLabel = (s) => {
    const n = Math.abs(s);
    return `${s < 0 ? '-' : '+'}${n >= 60 ? `${n / 60} min` : `${n} s`}`;
  };

  function checkFile(rid, onTick) {
    const started = Date.now();
    const timer = setInterval(() => onTick(Math.round((Date.now() - started) / 1000)), 1000);
    return jsonFetch(`/recordings/${rid}/file/check`).finally(() => clearInterval(timer));
  }

  function startDownload(info) {
    const a = document.createElement('a');
    a.href = info.download_url;
    a.download = info.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    showToast(`Downloading ${info.name} (${fmtBytes(info.size)}).`);
  }

  // The <video> element's own error, in words, with the codecs the file was measured to hold.
  function mediaErrorText(err, info) {
    const codecs = `video ${info.video_codec || 'unknown'}, audio ${info.audio_codec || 'unknown'}`;
    const detail = err && err.message ? ` (${err.message})` : '';
    const elsewhere = ' Download it and play it in VLC or another media player.';
    const caveat = info.caveat ? `\n${info.caveat}` : '';
    if (!err) return `Playback failed.${elsewhere}`;
    if (err.code === MediaError.MEDIA_ERR_NETWORK) {
      return `Lost the connection to ChannelBin while loading the file${detail}. Close this and press Play to try again.`;
    }
    if (err.code === MediaError.MEDIA_ERR_DECODE) {
      return `This browser could not decode part of the file: ${codecs}${detail}.${caveat}\n${elsewhere.trim()}`;
    }
    if (err.code === MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED) {
      return `This browser cannot play this file: ${codecs}${detail}.${caveat}\n${elsewhere.trim()}`;
    }
    return `Playback failed${detail}.${elsewhere}`;
  }

  function playNative(player, data, isClosed) {
    const { video, setStatus, tryPlay } = player;
    const woke = data.opened_seconds >= WOKE_NOTE_AFTER_S
      ? ` The storage took ${Math.round(data.opened_seconds)}s to answer.` : '';
    video.addEventListener('loadedmetadata', tryPlay, { once: true });
    video.addEventListener('playing', () => {
      if (!isClosed()) setStatus(`Playing ${data.name}.${woke}`, 'ok');
    });
    video.addEventListener('error', () => {
      if (!isClosed() && video.getAttribute('src')) setStatus(mediaErrorText(video.error, data), 'error');
    });
    setStatus(`Loading ${data.name}…${data.caveat ? `\n${data.caveat}` : ''}`);
    video.src = data.media_url;
  }

  // The preview pipeline, over a finished file or (data.capture) over the segments of a
  // recording still capturing. Returns { close } for the modal's onClose.
  function playPipeline(player, data, isClosed) {
    const { video, setStatus, tryPlay } = player;
    // A capture's timeline grows while it is written, so its length is asked of total().
    const capture = !!data.capture;
    const duration = !capture && data.duration > 0 ? data.duration : null;

    const controls = document.createElement('div');
    controls.className = 'player-seek';
    controls.innerHTML = `
      <input type="range" class="player-seek-bar" min="0" step="1" aria-label="Position in the recording"${duration || capture ? '' : ' hidden'}>
      <div class="player-seek-row">
        ${SKIPS.slice(0, 3).map((s) => `<button type="button" class="btn btn-sm" data-skip="${s}">${skipLabel(s)}</button>`).join('')}
        <button type="button" class="btn btn-sm player-pause">Pause</button>
        ${SKIPS.slice(3).map((s) => `<button type="button" class="btn btn-sm" data-skip="${s}">${skipLabel(s)}</button>`).join('')}
        ${capture ? '<button type="button" class="btn btn-sm player-live" hidden>Live</button>' : ''}
      </div>
      <div class="player-pos"></div>`;
    player.el.querySelector('.player-status').before(controls);
    const bar = controls.querySelector('.player-seek-bar');
    const pauseBtn = controls.querySelector('.player-pause');
    const liveBtn = controls.querySelector('.player-live');
    const posEl = controls.querySelector('.player-pos');
    if (duration) bar.max = String(Math.floor(duration));

    let session = null;       // the live session's start response
    let hls = null;
    let gen = 0;              // which start() a response belongs to
    let origin = 0;           // position in the recording at video.currentTime 0
    let held = 0;             // position while nothing is attached (starting, paused, seeking)
    let attached = false;
    let serverDone = false;   // the server reached the end of the file
    let finished = false;     // played to the end: Play starts again from the beginning
    let pending = null;       // a seek target still settling
    let dragging = false;
    let switching = false;    // our own detach, whose pause event is not the user's
    let recovered = false;
    let pollTimer = null;
    let seekTimer = null;
    let holdTimer = null;
    // A capture: how much had been captured when the server last said, and when.
    let captured = 0;
    let capturedAt = null;    // null until the first start has answered
    let capturing = !!data.live;
    let note = '';            // why the player moved on its own, shown with the playing line
    let retries = 0;

    const total = () => (capture
      ? captured + (capturing && capturedAt !== null ? (Date.now() - capturedAt) / 1000 : 0)
      : duration);
    const atLive = () => !!(session && session.capture && session.capture.live);
    const maxPos = () => (capture ? total() : (duration ? Math.max(0, duration - 1) : Infinity));
    const position = () => (attached ? origin + (video.currentTime || 0) : held);
    const clamp = (s) => Math.min(maxPos(), Math.max(0, s));

    const showPosition = () => {
      let at = pending !== null ? pending : (dragging ? Number(bar.value) : position());
      if (capture) {
        const tot = total();
        at = Math.min(at, tot);
        if (!dragging) bar.max = String(Math.floor(tot));
        // Nothing to say until the server has said how much has been captured.
        if (capturedAt === null) posEl.textContent = '';
        else if (atLive() && pending === null && !dragging) posEl.textContent = `Live - ${fmtDur(tot)} captured so far`;
        else posEl.textContent = `${fmtDur(at)} / ${fmtDur(tot)} captured so far`;
        if (liveBtn) {
          liveBtn.hidden = !capturing;
          liveBtn.classList.toggle('btn-primary', atLive());
        }
      } else {
        posEl.textContent = duration ? `${fmtDur(at)} / ${fmtDur(duration)}` : fmtDur(at);
      }
      if ((duration || capture) && !dragging) bar.value = String(Math.floor(at));
    };

    const playingText = () => {
      const audio = session && session.transcode_audio
        ? ' Audio is re-encoded to AAC so the browser can decode it.' : '';
      if (capture) {
        const where = atLive()
          ? 'Live, about ten seconds behind the capture.'
          : 'Playing what has been captured so far.';
        const why = [note, session && session.capture_note].filter(Boolean).join(' ');
        return `${where}${why ? ` ${why}` : ''} ChannelBin repackages the recording's ` +
          `segment files as they are written, without re-encoding the video, so skipping starts it again at the new spot.${audio}`;
      }
      return `Playing. ${data.reason || ''} ChannelBin repackages it for the browser as it plays, ` +
        `without re-encoding the video, so skipping starts it again at the new spot.${audio}`;
    };

    // Stop everything this player holds. `stopServer` asks the server to end the session too;
    // a seek does not, because starting the next session replaces it on the server first.
    const detach = ({ stopServer = true, beacon = false } = {}) => {
      clearTimeout(pollTimer);
      clearTimeout(holdTimer);
      // The position is read before hls.js lets go of the video, which resets its clock.
      if (attached) held = position();
      attached = false;
      switching = true;
      if (hls) { hls.destroy(); hls = null; }
      player.detach();
      switching = false;
      const s = session;
      session = null;
      if (!s || !stopServer) return;
      if (beacon && navigator.sendBeacon) {
        // pagehide cannot wait for a fetch and sendBeacon cannot set headers, so the token
        // travels as a form field, which the CSRF check accepts just the same.
        const fd = new FormData();
        fd.append('csrf_token', csrfToken());
        navigator.sendBeacon(s.stop_url, fd);
        return;
      }
      jsonFetch(s.stop_url, { method: 'POST' }).catch(() => {});
    };

    const ended = (s) => {
      if (s.reason === 'file_end') {
        // The last segments are already in the player's buffer; it plays them out and the
        // video's own ended event says so.
        serverDone = true;
        clearTimeout(pollTimer);
        if (video.ended) atEnd();
        return;
      }
      if (capture && (s.reason === 'segment_changed' || s.reason === 'capture_stopped')) {
        // The recorder left the file this session was following. Carry on from the same
        // place - live if it was live - in whatever file holds it now.
        const wasLive = atLive();
        const from = session && session.capture ? session.capture.segment_number : null;
        detach({ stopServer: false });
        const stopped = s.reason === 'capture_stopped' ? s.reason_text : '';
        start(wasLive ? null : held, { live: wasLive, movedFrom: from, stopped });
        return;
      }
      detach({ stopServer: false });
      const why = [s.reason_text, s.detail].filter(Boolean).join('\n');
      setStatus(why || 'Stopped.', s.reason === 'user' ? '' : 'error');
      pauseBtn.textContent = 'Play';
    };

    const atEnd = () => {
      const spanEnd = capture && session && session.capture ? session.capture.span_end : null;
      if (spanEnd !== null && (capturing || spanEnd < captured - 1)) {
        // The end of one captured segment: the next one carries on from there.
        detach();
        note = '';
        start(spanEnd);
        return;
      }
      detach();
      held = (capture ? total() : duration) || held;
      finished = true;
      pauseBtn.textContent = 'Play';
      setStatus(capture
        ? 'Reached the end of what has been captured so far. Press Play to watch it again from the start.'
        : 'Reached the end of the recording. Press Play to watch it again from the start.');
      showPosition();
    };

    const onHlsError = (_evt, d) => {
      if (!d || !d.fatal || !hls) return;
      const Hls = window.Hls;
      if (d.type === Hls.ErrorTypes.MEDIA_ERROR && !recovered) {
        recovered = true;
        hls.recoverMediaError();
        return;
      }
      if (d.type === Hls.ErrorTypes.NETWORK_ERROR && serverDone) {
        // The server's session ended at the end of the file before the player had every
        // segment (a long pause near the end). Start again where it stopped.
        start(position());
        return;
      }
      detach({ stopServer: d.type !== Hls.ErrorTypes.NETWORK_ERROR });
      pauseBtn.textContent = 'Play';
      const codecs = `video ${data.video_codec || 'unknown'}, audio ${data.audio_codec || 'unknown'}`;
      if (d.type === Hls.ErrorTypes.MEDIA_ERROR) {
        setStatus(`This browser cannot decode this file: ${codecs} (${d.details}).${data.caveat ? `\n${data.caveat}` : ''}\nDownload it and play it in VLC or another media player.`, 'error');
      } else if (d.type === Hls.ErrorTypes.NETWORK_ERROR) {
        setStatus(`Lost the connection to ChannelBin while playing (${d.details}). Press Play to try again.`, 'error');
      } else {
        setStatus(`Playback failed (${d.details}). Press Play to try again.`, 'error');
      }
    };

    let originFound = false;
    const onInitPts = (_evt, d) => {
      if (!d || !d.timescale) return;
      const base = typeof d.initPTS === 'object' && d.initPTS ? d.initPTS.baseTime : d.initPTS;
      // The server's timestamps ARE positions in the file it reads (proc_utils.
      // build_file_playback_cmd), so the first one is where this session landed - offset by
      // where that file starts in the recording when it is one of a capture's segments.
      const fileStart = session && session.capture ? session.capture.base : 0;
      const at = fileStart + base / d.timescale;
      if (!originFound && Number.isFinite(at) && Math.abs(at - held) <= ORIGIN_TRUST_S) {
        originFound = true;
        origin = at;
      }
    };

    const attach = (token) => {
      originFound = false;
      attachHls(video, session.playlist_url, {
        src: HLS_SRC,
        // startPosition 0: begin at the first segment, which is the requested position,
        // rather than a live edge the burst has already moved past.
        config: { startPosition: 0, liveSyncDurationCount: 3 },
        onReady: tryPlay,
        onError: onHlsError,
        onInitPts,
      })
        .then((h) => {
          if (isClosed() || token !== gen) { if (h) h.destroy(); return; }
          hls = h;
          origin = held;
          attached = true;
        })
        .catch((e) => {
          if (!isClosed() && token === gen) setStatus(e.message, 'error');
        });
    };

    const poll = (token) => {
      if (isClosed() || token !== gen || !session) return;
      jsonFetch(session.status_url)
        .then((s) => {
          if (isClosed() || token !== gen || !session) return;
          if (s.state === 'STOPPED') { ended(s); return; }
          if (s.state === 'READY' && !attached && !hls) {
            setStatus(atLive() ? 'Starting live…' : `Starting at ${fmtDur(held)}…`);
            attach(token);
          }
          if (s.source_done) serverDone = true;
          pollTimer = setTimeout(() => poll(token), attached ? 3000 : 500);
        })
        .catch((e) => {
          if (!isClosed() && token === gen) setStatus(e.message || 'Lost contact with ChannelBin.', 'error');
        });
    };

    // `at` is a position in the recording; for a capture, `live` starts at the live edge
    // instead, `movedFrom` names the segment a follower was on when the recorder left it (the
    // answer then says why, from the recording's own events), and `stopped` is the server's
    // words for a capture that stopped under the player.
    function start(at, { live = false, movedFrom = null, stopped = '' } = {}) {
      const token = ++gen;
      clearTimeout(seekTimer);
      pending = null;
      detach({ stopServer: false });
      held = live ? total() : clamp(at);
      finished = false;
      serverDone = false;
      recovered = false;
      pauseBtn.textContent = 'Pause';
      setStatus(live ? 'Starting live…' : `Starting at ${fmtDur(held)}…`);
      showPosition();
      const body = { offset: held };
      if (live) body.live = true;
      if (movedFrom !== null) body.moved_from = movedFrom;
      jsonFetch(data.playback_url, { method: 'POST', body: JSON.stringify(body) })
        .then((s) => {
          if (isClosed() || token !== gen) {
            jsonFetch(s.stop_url, { method: 'POST' }).catch(() => {});
            return;
          }
          session = s;
          retries = 0;
          if (capture) {
            captured = s.captured_seconds || 0;
            capturedAt = Date.now();
            capturing = !!s.capturing;
            if (s.capture && s.capture.live) held = captured;
            note = s.moved_because ? `The recording moved to a new segment file: ${s.moved_because}` : '';
            if (stopped) note = `${stopped} ${note}`.trim();
            showPosition();
          }
          poll(token);
        })
        .catch((e) => {
          if (isClosed() || token !== gen) return;
          // A capture between segments, or not started yet, answers 409: ask again while it
          // is capturing, saying what the server said.
          if (capture && capturing && !stopped && e.status === 409 && retries < CAPTURE_RETRIES) {
            retries += 1;
            setStatus(`${e.message} Trying again…`);
            seekTimer = setTimeout(() => { if (token === gen) start(at, { live, movedFrom }); },
              CAPTURE_RETRY_MS);
            return;
          }
          retries = 0;
          pauseBtn.textContent = 'Play';
          if (stopped) {
            capturing = false;
            setStatus(`${stopped} ${errorText(e)}\nClose this and press Play again once it is ready.`, 'warn');
            showPosition();
            return;
          }
          setStatus(errorText(e), capture && e.status ? 'warn' : 'error');
        });
    }

    const seekTo = (at) => {
      pending = clamp(at);
      showPosition();
      clearTimeout(seekTimer);
      seekTimer = setTimeout(() => { note = ''; start(pending); }, SEEK_SETTLE_MS);
    };

    controls.addEventListener('click', (e) => {
      const skip = e.target.closest('[data-skip]');
      if (!skip) return;
      seekTo((pending !== null ? pending : position()) + Number(skip.dataset.skip));
    });
    if (liveBtn) liveBtn.addEventListener('click', () => { note = ''; start(null, { live: true }); });
    bar.addEventListener('input', () => { dragging = true; showPosition(); });
    bar.addEventListener('change', () => { dragging = false; seekTo(Number(bar.value)); });
    pauseBtn.addEventListener('click', () => {
      if (attached && !video.paused) video.pause();
      else if (attached) tryPlay();
      else start(finished ? 0 : held);
    });

    video.addEventListener('timeupdate', showPosition);
    video.addEventListener('playing', () => {
      if (isClosed() || !attached) return;
      pauseBtn.textContent = 'Pause';
      const woke = data.opened_seconds >= WOKE_NOTE_AFTER_S
        ? ` The storage took ${Math.round(data.opened_seconds)}s to answer.` : '';
      setStatus(playingText() + woke, 'ok');
    });
    video.addEventListener('pause', () => {
      if (switching || isClosed() || !attached || video.ended) return;
      pauseBtn.textContent = 'Play';
      clearTimeout(holdTimer);
      holdTimer = setTimeout(() => {
        detach();
        pauseBtn.textContent = 'Play';
        setStatus(`Paused at ${fmtDur(held)}. Press Play to carry on from there.`);
        showPosition();
      }, PAUSE_HOLD_S * 1000);
    });
    video.addEventListener('play', () => {
      clearTimeout(holdTimer);
      // The browser's own play button on a player whose session was let go.
      if (!switching && !attached && !session && !isClosed()) start(finished ? 0 : held);
    });
    video.addEventListener('ended', () => { if (attached) atEnd(); });

    const onPageHide = () => detach({ beacon: true });
    window.addEventListener('pagehide', onPageHide);

    if (capture && data.live) start(null, { live: true });
    else start(0);
    return {
      close: () => {
        gen += 1;
        clearTimeout(seekTimer);
        window.removeEventListener('pagehide', onPageHide);
        detach();
      },
    };
  }

  function openPlayer(rid) {
    const player = buildPlayerStage();
    const { setStatus } = player;
    let closed = false;
    let info = null;
    let pipeline = null;
    const isClosed = () => closed;

    const modal = buildModal({
      title: `Play - ${recName()}`,
      body: player.el,
      panelClass: 'modal-wide player-modal',
      footer: [{ label: 'Download', class: 'btn', onClick: () => { if (info) startDownload(info); return false; } }],
      onClose: () => {
        closed = true;
        if (pipeline) pipeline.close();
        player.detach();
      },
    });
    const dlBtn = modal.querySelector('.modal-foot .btn');
    dlBtn.disabled = true;

    setStatus(waitingText(0));
    checkFile(rid, (secs) => { if (!closed && !info) setStatus(waitingText(secs)); })
      .then((data) => {
        if (closed) return;
        info = data;
        // Nothing to download while the recording is still capturing.
        dlBtn.disabled = data.download === false;
        dlBtn.hidden = data.download === false;
        if (data.native) playNative(player, data, isClosed);
        else if (data.pipeline || data.capture) pipeline = playPipeline(player, data, isClosed);
        else setStatus(data.reason, 'warn');
      })
      .catch((e) => {
        if (!closed) setStatus(errorText(e), 'error');
      });
    return modal;
  }

  function download(rid) {
    showToast('Opening the file - a drive that has spun down can take ten seconds or more to wake.',
      { durationMs: 60000 });
    checkFile(rid, () => {})
      .then(startDownload)
      .catch((e) => showToast(errorText(e), { type: 'error', durationMs: 10000 }));
  }

  window.recordingDialogs = Object.assign(window.recordingDialogs || {},
    { play: openPlayer, download });
})();
