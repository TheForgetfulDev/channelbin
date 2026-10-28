/* The live preview of one channel, in the shared player (DESIGN.md 3.18, dev/changelog/1018).

   The server runs one ffmpeg that stream-copies the channel into a rolling HLS window
   (app/preview.py); this plays it. The browser never sees the stream URL, only the session's
   playlist. Every way the dialog can go away stops the session: the Stop button and the
   close/Esc/backdrop paths all run onClose, and leaving the page fires a pagehide beacon. The
   server's idle reaper is the backstop for a tab that dies without either, so a stop here is
   prompt rather than load-bearing.

   Called by the channel page's Preview and by the TV Guide's program chooser. The hls.js
   URL comes from this script tag's data-hls-js: it is loaded on the first Preview, not with
   the page - 386 KB nobody pays for until they ask to watch something
   (static/vendor/hls.js/NOTICE.md). */

(() => {
  const HLS_SRC = (document.currentScript && document.currentScript.dataset.hlsJs) || '';

  function openLivePreview({ channelId, channelName }) {
    const player = buildPlayerStage();
    const { video, setStatus, tryPlay } = player;
    setStatus('Starting…');

    let session = null;     // the start response: status_url / playlist_url / stop_url
    let hls = null;
    let pollTimer = null;
    let closed = false;
    let recoveredOnce = false;

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
        window.removeEventListener('pagehide', onPageHide);
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

    video.addEventListener('playing', () => {
      if (closed || !session) return;
      setStatus(session.transcode_audio
        ? 'Playing. Audio is re-encoded to AAC so the browser can decode it; video is untouched.'
        : 'Playing.', 'ok');
    });

    const onHlsError = (_evt, data) => {
      if (!data || !data.fatal) return;
      if (data.type === Hls.ErrorTypes.MEDIA_ERROR && !recoveredOnce) {
        recoveredOnce = true;
        hls.recoverMediaError();
        return;
      }
      if (hls) { hls.destroy(); hls = null; }
      if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
        setStatus(`This browser cannot decode this stream (${data.details}). HEVC video, or an audio codec the browser lacks, is the usual reason.`, 'error');
      } else if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
        // The status poll will replace this with the server's reason if the session ended.
        setStatus(`Lost the stream (${data.details}).`, 'error');
      } else {
        setStatus(`Playback failed (${data.details}).`, 'error');
      }
    };

    const attach = () => {
      attachHls(video, session.playlist_url, {
        src: HLS_SRC,
        config: { liveSyncDurationCount: 3, liveMaxLatencyDurationCount: 8 },
        onReady: tryPlay,
        onError: onHlsError,
      })
        .then((h) => {
          if (closed) { if (h) h.destroy(); return; }
          hls = h;
        })
        .catch((e) => {
          if (!closed) setStatus(e.message || 'Could not load the video player script.', 'error');
        });
    };

    // hls.js gives a missing manifest very few retries, so the playlist is only handed to
    // it once the server says the first segment exists; the poll keeps running afterwards
    // so a stop decided on the server (preempted, idle, ended) reaches the dialog by name.
    let attached = false;
    const poll = () => {
      if (closed || !session) return;
      jsonFetch(session.status_url)
        .then((s) => {
          if (closed || !session) return;
          if (s.state === 'STOPPED') { ended(s); return; }
          if (s.state === 'READY' && !attached) {
            attached = true;
            setStatus('Connected - starting playback…');
            attach();
          } else if (s.state === 'STARTING') {
            setStatus(`Connecting to the stream… (${Math.round(s.elapsed_seconds)}s)`);
          }
          pollTimer = setTimeout(poll, attached ? 3000 : 1000);
        })
        .catch((e) => {
          if (closed) return;
          setStatus(e.message || 'Lost contact with the preview.', 'error');
        });
    };

    jsonFetch(`/api/channels/${encodeURIComponent(channelId)}/preview`, { method: 'POST' })
      .then((data) => {
        if (closed) {
          // Closed before the start round-trip came back: stop what was just started.
          session = data;
          stopSession();
          return;
        }
        session = data;
        setStatus('Connecting to the stream…');
        poll();
      })
      .catch((e) => {
        if (closed) return;
        setStatus(e.message || 'Could not start the preview.', 'error');
      });

    return modal;
  }

  window.openLivePreview = openLivePreview;
})();
