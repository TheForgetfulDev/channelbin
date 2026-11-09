/* The live preview of one channel, in the shared player (DESIGN.md 3.18, dev/changelog/1018).

   The server runs one ffmpeg that stream-copies the channel into a rolling HLS window
   (app/preview.py); this plays it. The browser never sees the stream URL, only the session's
   playlist. Every way the dialog can go away stops the session: the Stop button and the
   close/Esc/backdrop paths all run onClose, and leaving the page fires a pagehide beacon. The
   server's idle reaper is the backstop for a tab that dies without either, so a stop here is
   prompt rather than load-bearing.

   When the browser cannot decode the copy, the dialog says what the browser said and offers
   a re-encode on the GPU, which starts a new session that re-encodes the video
   (dev/changelog/1461). Nothing re-encodes unless the viewer asks.

   Called by the channel page's Preview and by the TV Guide's program chooser. The hls.js
   URL comes from this script tag's data-hls-js: it is loaded on the first Preview, not with
   the page - 620 KB nobody pays for until they ask to watch something
   (static/vendor/hls.js/NOTICE.md). */

(() => {
  const HLS_SRC = (document.currentScript && document.currentScript.dataset.hlsJs) || '';

  function openLivePreview({ channelId, channelName }) {
    const player = buildPlayerStage();
    const { video, setStatus, tryPlay } = player;

    let session = null;     // the start response: status_url / playlist_url / stop_url
    let hls = null;
    let pollTimer = null;
    let closed = false;
    let recoveredOnce = false;
    let failed = false;     // this session's decode failure has been reported
    let attached = false;
    let gen = 0;            // which begin() a response belongs to

    // pagehide cannot wait for a fetch and sendBeacon cannot set headers, so the token
    // travels as a form field, which the CSRF check accepts just the same.
    const stopSession = ({ beacon = false } = {}) => {
      if (!session) return;
      const url = session.stop_url;
      session = null;
      if (beacon && navigator.sendBeacon) {
        const fd = new FormData();
        fd.append('csrf_token', csrfToken());
        navigator.sendBeacon(url, fd);
        return;
      }
      jsonFetch(url, { method: 'POST' }).catch(() => {});
    };
    const onPageHide = () => stopSession({ beacon: true });

    const detachPlayer = () => {
      clearTimeout(pollTimer);
      if (hls) { hls.destroy(); hls = null; }
      player.detach();
    };

    const modal = buildModal({
      title: `Preview - ${channelName}`,
      body: player.el,
      panelClass: 'modal-wide player-modal',
      footer: [{ label: 'Stop', class: 'btn', onClick: (close) => close() }],
      onClose: () => {
        closed = true;
        gen += 1;
        window.removeEventListener('pagehide', onPageHide);
        player.clearOffer();
        detachPlayer();
        stopSession();
      },
    });
    window.addEventListener('pagehide', onPageHide);

    // The session ended on the server. Name why - a preview that stops without saying is
    // a number nobody can explain - and leave the dialog up so the reason can be read.
    const ended = (s) => {
      detachPlayer();
      session = null;
      const why = [s.reason_text, s.detail].filter(Boolean).join('\n');
      setStatus(why || 'Stopped.', s.reason === 'user' ? '' : 'error');
    };

    // The browser could not decode what it was given. Its own words, then the re-encode -
    // unless this already was the re-encode, which leaves nothing further to offer.
    const undecodable = (hlsData) => {
      if (failed) return;
      failed = true;
      const said = playbackFailureText(video, hlsData);
      const reencoded = session && session.reencode;
      // Nothing more will play from this session, so its provider connection is let go now.
      detachPlayer();
      stopSession();
      setStatus(`This browser could not play ${reencoded ? 'the re-encoded video' : 'this stream'}: ${said}.`, 'error');
      if (!reencoded) player.offerReencode(() => begin(true));
    };

    video.addEventListener('playing', () => {
      if (closed || !session) return;
      let text = 'Playing.';
      if (session.reencode) {
        text = 'Playing. ChannelBin re-encodes the video on the GPU, and the audio to AAC, so this browser can play it.';
      } else if (session.transcode_audio) {
        text = 'Playing. Audio is re-encoded to AAC so the browser can decode it; video is untouched.';
      }
      setStatus(text, 'ok');
    });
    // The decoder refusing a packet puts the element in error at once, and hls.js may never
    // say so: on channel 44107's segments in Chrome 155 the element errored at 0.5 s and hls.js
    // raised nothing at all in 15 s (dev/changelog/1461). So the element's own error is acted
    // on too: one recovery, as for hls.js's, then the failure is reported. The browser's own
    // HLS (Safari) reports only here.
    const recoverOrFail = (hlsData) => {
      if (hls && !recoveredOnce) {
        recoveredOnce = true;
        hls.recoverMediaError();
        return;
      }
      undecodable(hlsData);
    };
    video.addEventListener('error', () => {
      if (closed || !session || !attached || !video.error) return;
      if (video.error.code === MediaError.MEDIA_ERR_NETWORK) return;   // the poll names a stop
      recoverOrFail(null);
    });

    const onHlsError = (_evt, data) => {
      if (!data || !data.fatal) return;
      if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
        recoverOrFail(data);
        return;
      }
      if (hls) { hls.destroy(); hls = null; }
      if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
        // The status poll will replace this with the server's reason if the session ended.
        setStatus(`Lost the stream (${data.details}).`, 'error');
      } else {
        setStatus(`Playback failed: ${playbackFailureText(video, data)}.`, 'error');
      }
    };

    const attach = (token) => {
      attachHls(video, session.playlist_url, {
        src: HLS_SRC,
        config: { liveSyncDurationCount: 3, liveMaxLatencyDurationCount: 8 },
        onReady: tryPlay,
        onError: onHlsError,
      })
        .then((h) => {
          if (closed || token !== gen) { if (h) h.destroy(); return; }
          hls = h;
        })
        .catch((e) => {
          if (!closed && token === gen) setStatus(e.message || 'Could not load the video player script.', 'error');
        });
    };

    // hls.js gives a missing manifest very few retries, so the playlist is only handed to
    // it once the server says the first segment exists; the poll keeps running afterwards
    // so a stop decided on the server (preempted, idle, ended) reaches the dialog by name.
    const poll = (token) => {
      if (closed || !session || token !== gen) return;
      jsonFetch(session.status_url)
        .then((s) => {
          if (closed || !session || token !== gen) return;
          if (s.state === 'STOPPED') { ended(s); return; }
          if (s.state === 'READY' && !attached) {
            attached = true;
            setStatus('Connected - starting playback…');
            attach(token);
          } else if (s.state === 'STARTING') {
            setStatus(`Connecting to the stream… (${Math.round(s.elapsed_seconds)}s)`);
          }
          pollTimer = setTimeout(() => poll(token), attached ? 3000 : 1000);
        })
        .catch((e) => {
          if (closed || token !== gen) return;
          setStatus(e.message || 'Lost contact with the preview.', 'error');
        });
    };

    // Start a session - a stream copy, or the GPU re-encode the viewer asked for. A new one
    // replaces the old on the server before it starts, so the old one is only let go here.
    function begin(reencode) {
      const token = ++gen;
      detachPlayer();
      session = null;
      attached = false;
      recoveredOnce = false;
      failed = false;
      setStatus(reencode ? 'Starting the re-encode…' : 'Starting…');
      const opts = { method: 'POST' };
      if (reencode) opts.body = JSON.stringify({ reencode: true });
      jsonFetch(`/api/channels/${encodeURIComponent(channelId)}/preview`, opts)
        .then((data) => {
          if (closed || token !== gen) {
            // Closed or replaced before the start round-trip came back: stop what was just started.
            jsonFetch(data.stop_url, { method: 'POST' }).catch(() => {});
            return;
          }
          session = data;
          setStatus(reencode ? 'Connecting to the stream to re-encode it…' : 'Connecting to the stream…');
          poll(token);
        })
        .catch((e) => {
          if (closed || token !== gen) return;
          setStatus(e.message || 'Could not start the preview.', 'error');
        });
    }

    begin(false);
    return modal;
  }

  window.openLivePreview = openLivePreview;
})();
