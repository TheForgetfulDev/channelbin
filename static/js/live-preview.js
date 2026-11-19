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

   A feed preview ({ feedId }) plays one of ChannelBin's own channels on the IPTV output as an
   IPTV player gets it (dev/changelog/1645). The server starts a new round of segments each time
   the output's play moves to another source - the waiting screen handing over to the game, a
   move to another channel - and the status names the round, so the player lets go of the old
   round and attaches to the new one, saying what is on now.

   Called by the channel page's Preview, the TV Guide's program chooser, the IPTV Output page, the
   sports pages and a channel group's page. The hls.js
   URL comes from this script tag's data-hls-js: it is loaded on the first Preview, not with
   the page - 620 KB nobody pays for until they ask to watch something
   (static/vendor/hls.js/NOTICE.md). */

(() => {
  const HLS_SRC = (document.currentScript && document.currentScript.dataset.hlsJs) || '';

  function openLivePreview({ channelId, feedId, channelName }) {
    const startUrl = feedId !== undefined && feedId !== null
      ? `/api/feed/${encodeURIComponent(feedId)}/preview`
      : `/api/channels/${encodeURIComponent(channelId)}/preview`;
    const player = buildPlayerStage();
    const { video, setStatus, tryPlay } = player;

    let session = null;     // the start response: status_url / playlist_url / stop_url
    let hls = null;
    let pollTimer = null;
    let closed = false;
    let recoveredOnce = false;
    let failed = false;     // this session's decode failure has been reported
    let attached = false;
    let attachedRound = null;   // a feed preview's round the player is on
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
      if (session.source === 'feed') {
        text = `Playing what an IPTV player gets: ${session.now_playing || 'the stream'}. Its sound is re-encoded to AAC so this browser can play it.`;
        if (session.reencode) text += ' ChannelBin re-encodes the video on the GPU so this browser can play it.';
      } else if (session.reencode) {
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
          session.now_playing = s.now_playing;
          if (attached && s.source === 'feed' && (s.state === 'STARTING' || s.round !== attachedRound)) {
            // The output moved to another source: this round is over, and the next one starts
            // with a decoder of its own.
            detachPlayer();
            attached = false;
            recoveredOnce = false;
          }
          if (s.state === 'READY' && !attached) {
            attached = true;
            attachedRound = s.round;
            session.playlist_url = s.playlist_url;
            setStatus(attachedRound > 1 ? `Switching to ${s.now_playing}…` : 'Connected - starting playback…');
            attach(token);
          } else if (s.state === 'STARTING' && s.source === 'feed' && attachedRound !== null) {
            setStatus(`The IPTV output moved to another source. Switching to it…`);
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
      attachedRound = null;
      recoveredOnce = false;
      failed = false;
      setStatus(reencode ? 'Starting the re-encode…' : 'Starting…');
      const opts = { method: 'POST' };
      if (reencode) opts.body = JSON.stringify({ reencode: true });
      jsonFetch(startUrl, opts)
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

  /* A Preview button drawn by a page (the IPTV Output page's channel rows, the league page's
     sports channels, the sports dialogs, a channel group's page): data-feed for one of
     ChannelBin's own channels, data-channel for a provider's, data-name for the dialog's title,
     data-why when there is nothing to preview, which is said instead. */
  function previewFrom(el) {
    const d = el.dataset;
    if (d.why) { showToast(d.why, { type: 'warning' }); return null; }
    // A dialog that names the channel closes first, as the TV Guide's program chooser does, so
    // the player is the only dialog up and Escape closes only it.
    const host = el.closest('.modal');
    if (host && host.closeModal) host.closeModal();
    if (d.feed) return openLivePreview({ feedId: Number(d.feed), channelName: d.name || '' });
    return openLivePreview({ channelId: Number(d.channel), channelName: d.name || '' });
  }

  /* The same button as templates/_macros.html::preview_btn, for a provider's channel a script
     draws (the sports pages' dialogs). */
  // duplicated from templates/_macros.html::preview_btn - a dialog's markup is built in JS
  const previewButton = ({ channelId, name }) => `<button class="btn btn-sm tip-plain" data-act="preview" data-channel="${escHtml(String(channelId))}" data-name="${escHtml(name || '')}" data-tip="Preview.&#10;Plays this channel's live stream right here. Holds one of its account's connections while it runs, and gives it up to any recording that needs it.">${icon('preview')}Preview</button>`;

  window.openLivePreview = openLivePreview;
  window.previewFrom = previewFrom;
  window.previewButton = previewButton;
})();
