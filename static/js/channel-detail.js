// Channel detail page (templates/channels/detail.html).
// Design: dev/mockups/16-channel-detail-revamp.html (desktop) + 20-channel-detail-mobile.html
// (mobile), approved 2026-07-26; rounds logged in dev/changelog/339-342 and 347.
//
// Deliberately mirrors static/js/group-detail.js: the section-layout machinery, the
// settings-bar/settings-modal pair and the action dispatch are the same components on both
// pages, so the two read as one system. Shared helpers (buildModal, fieldRow, escHtml,
// showToast, jsonFetch) come from util.js and are never re-declared here.
(() => {
  'use strict';

  const C = window.CHANNEL_DETAIL;
  const byId = (id) => document.getElementById(id);

  function showActionError(msg) {
    const el = byId('cd-action-error');
    if (!el) return;
    el.textContent = msg;
    el.style.display = msg ? '' : 'none';
  }

  // ── Section layout (order + hidden), persisted server-side ────────────────
  // Same mechanism as the group page: a generated stylesheet keyed on [data-section]
  // over a flex column, so nothing in the page's DOM is moved or rebuilt.

  const SEC_NAMES = {
    health: 'Channel Health', settings: 'Settings', whatson: "What's On",
    timeline: 'Activity Timeline', url: 'Stream URL',
    recordings: 'Recording Observations', tests: 'Test History',
  };
  const sectionLayout = initSectionLayout({
    config: C,
    saveUrl: C.urls.sectionPref,
    names: SEC_NAMES,
    note: 'The title, status bar and the recovery notice always show.',
  });

  // ── Settings bar + modal ──────────────────────────────────────────────────
  // The chips and the modal read the same state object, so a chip can never show a
  // value the modal would disagree with.

  const state = {
    profileId: C.defaultProfileId,
    profileName: C.defaultProfileName,
    adjustment: C.healthAdjustment,
    healthNote: C.healthNote,
    notes: C.notes,
    testEnabled: C.testEnabled,
    paceRealtime: C.paceRealtime,
    checks: (C.healthChecks || []).slice(),
  };

  function enrollmentValue() {
    if (!state.checks.length) return 'Not enrolled';
    return state.checks.length === 1 ? state.checks[0] : `${state.checks.length} checks`;
  }

  // null follows ffmpeg.pace_realtime in Settings, so the chip names the default it follows.
  function paceValue() {
    if (state.paceRealtime === null) return `Default (${C.paceRealtimeDefault ? 'on' : 'off'})`;
    return state.paceRealtime ? 'On' : 'Off';
  }

  // `note` is the one-line "what is this setting" the mobile list row shows under the label
  // (mockup 20). Desktop's chip hides it in CSS rather than a second item list, so the two
  // arrangements cannot drift into describing different settings.
  function settingItems() {
    return [
      { focus: 'profile', label: 'Profile', value: state.profileName || 'None',
        note: 'Pre-selected when scheduling from the guide',
        tip: 'The recording profile pre-selected when you schedule this channel from the guide. It can still be changed per recording.' },
      { focus: 'pace', label: 'Real-time read', value: paceValue(),
        note: 'Record no faster than the stream plays',
        tip: 'Whether recordings read this stream at real-time speed, the way a video player does. Helps a provider that re-sends its buffered video on every reconnect.' },
      { focus: 'health', label: 'Health offset',
        value: state.adjustment ? (state.adjustment > 0 ? `+${state.adjustment}` : String(state.adjustment)) : 'None',
        note: 'Nudges the computed score',
        tip: 'Nudges the computed health score up or down (-100 to +100). The adjusted score is what ranks this channel against the others in its group.' },
      { focus: 'enroll', label: 'Testing', value: enrollmentValue(),
        note: 'Health checks that test this channel on a schedule',
        tip: 'Which health checks test this channel on a schedule.' },
      { focus: 'notes', label: 'Notes', value: state.notes ? 'Note added' : 'None',
        note: 'Freeform notes about this channel',
        tip: 'Your own notes about this channel.' },
    ];
  }

  function renderSettingsBar() {
    byId('cd-setbar').innerHTML = settingItems().map(it => {
      const inner = '<span class="gd-chip-main">' +
                    `<span class="gd-chip-lbl">${escHtml(it.label)}</span>` +
                    `<span class="gd-chip-note">${escHtml(it.note)}</span></span>` +
                    `<span class="gd-chip-val">${escHtml(it.value)}</span>`;
      const tip = `${escHtml(it.label)}.&#10;${escHtml(it.tip)} Click to change it.`;
      return `<button type="button" class="gd-chip tip-plain" data-act="settings" data-focus="${it.focus}"` +
             ` data-tip="${tip}">${inner}</button>`;
    }).join('');
  }

  function openSettingsModal(focus) {
    const body = document.createElement('div');
    const profileOpts = ['<option value="">None (use global defaults)</option>'].concat(
      C.profiles.map(p => `<option value="${p.id}"${p.id === state.profileId ? ' selected' : ''}>${escHtml(p.name)}</option>`)
    ).join('');

    let h = '';
    h += `<fieldset class="gd-fset${focus === 'profile' || focus === 'pace' ? ' hi' : ''}"><div class="gd-fset-head">Recording</div>`;
    h += fieldRow({
      label: 'Default recording profile',
      meta: 'Pre-selected when scheduling a recording for this channel from the TV Guide. It can still ' +
        'be changed per recording in the record modal.',
      control: `<select id="cd-profile">${profileOpts}</select>`,
    });
    const paceOpts = [
      ['default', `Default (${C.paceRealtimeDefault ? 'on' : 'off'})`],
      ['on', 'On'],
      ['off', 'Off'],
    ];
    const paceSel = state.paceRealtime === null ? 'default' : (state.paceRealtime ? 'on' : 'off');
    h += fieldRow({
      label: 'Read at real-time speed',
      meta: 'Record no faster than the stream plays, the way a video player does. Most channels record ' +
        'the same either way; it helps a provider that answers every reconnect by sending its buffered ' +
        'video again. Default follows the setting in Settings. With Default and that setting off, a ' +
        'recording still switches this on by itself when the replay pattern shows up; Off stops that too.',
      control: `<select id="cd-pace">${paceOpts.map(([v, l]) =>
        `<option value="${v}"${v === paceSel ? ' selected' : ''}>${l}</option>`).join('')}</select>`,
    });
    h += '</fieldset>';

    h += `<fieldset class="gd-fset${focus === 'health' ? ' hi' : ''}"><div class="gd-fset-head">Health</div>`;
    h += fieldRow({
      label: 'Manual health offset',
      meta: 'Nudges the computed score up or down (-100 to +100). The adjusted score is what ranks this ' +
        'channel against the other channels in its group.',
      control: `<input type="number" id="cd-adjust" min="-100" max="100" step="1" name="adjustment" value="${state.adjustment}">`,
    });
    h += fieldRow({
      label: 'Reason',
      meta: 'Optional - why the score was nudged. Shown under the score on this page.',
      wide: true,
      control: `<input type="text" id="cd-adjust-note" value="${escHtml(state.healthNote || '')}">`,
    });
    h += '</fieldset>';

    h += `<fieldset class="gd-fset${focus === 'enroll' ? ' hi' : ''}"><div class="gd-fset-head">Testing</div>`;
    if (C.guideCheckJobId) {
      h += fieldRow({
        label: 'Test with the guide check',
        meta: 'The automatic "TV Guide Channels" health check tests one channel per guide row - a standalone ' +
          'channel like this one, or the member currently serving a group\'s row. Turn this off to leave this ' +
          'channel out of that run - it does not affect any health check you built yourself.',
        control: `<label class="switch"><input type="checkbox" id="cd-test-enabled"${state.testEnabled ? ' checked' : ''}><span class="knob"></span></label>`,
      });
    }
    h += fieldRow({
      label: 'Health checks',
      meta: state.checks.length
        ? `Tested by ${state.checks.map(n => `<strong>${escHtml(n)}</strong>`).join(', ')}. ` +
          'Enrollment is edited from the check itself - the chips in the title bar link to each one.'
        : 'No health check tests this channel on a schedule, so a provider-side change would not be ' +
          'caught automatically. Create one from the &#8943; menu.',
      control: '',
      full: true,
    });
    h += '</fieldset>';

    h += `<fieldset class="gd-fset${focus === 'notes' ? ' hi' : ''}"><div class="gd-fset-head">Notes</div>`;
    h += fieldRow({
      label: 'Notes',
      meta: 'Anything you want to remember about this channel. A note puts a &#128221; marker in the title bar.',
      full: true,
      control: `<textarea id="cd-notes" class="form-control" rows="4" style="resize:vertical; width:100%">${escHtml(state.notes || '')}</textarea>`,
    });
    h += '</fieldset>';

    body.innerHTML = h;

    const modal = buildModal({
      title: 'Settings',
      body,
      footer: [
        { label: 'Cancel', class: 'btn' },
        { label: 'Save', class: 'btn btn-primary', onClick: (close) => { saveSettings(body, close); return false; } },
      ],
    });
    modal.querySelector('.modal-panel').classList.add('modal-wide');
  }

  // Each block is its own request - the profile, the pacing, the offset, the notes and the
  // guide-check enrollment are five different endpoints, and one failing must not silently swallow the
  // others. Only what actually changed is sent.
  function saveSettings(body, close) {
    const profileRaw = body.querySelector('#cd-profile').value;
    const profileId = profileRaw === '' ? null : Number(profileRaw);
    const adjustment = Math.max(-100, Math.min(100, Number(body.querySelector('#cd-adjust').value) || 0));
    const healthNote = body.querySelector('#cd-adjust-note').value.trim();
    const notes = body.querySelector('#cd-notes').value;
    const testToggle = body.querySelector('#cd-test-enabled');
    const paceRaw = body.querySelector('#cd-pace').value;
    const paceRealtime = paceRaw === 'default' ? null : paceRaw === 'on';

    const reqs = [];
    if (profileId !== state.profileId) {
      reqs.push(jsonFetch(C.urls.defaultProfile, {
        method: 'POST', body: JSON.stringify({ profile_id: profileId }),
      }).then(() => {
        state.profileId = profileId;
        const p = C.profiles.find(x => x.id === profileId);
        state.profileName = p ? p.name : null;
      }));
    }
    if (paceRealtime !== state.paceRealtime) {
      reqs.push(jsonFetch(C.urls.paceRealtime, {
        method: 'POST', body: JSON.stringify({ pace_realtime: paceRealtime }),
      }).then(() => { state.paceRealtime = paceRealtime; }));
    }
    if (adjustment !== state.adjustment || healthNote !== (state.healthNote || '')) {
      reqs.push(jsonFetch(C.urls.healthAdjustment, {
        method: 'POST', body: JSON.stringify({ adjustment, note: healthNote }),
      }).then(() => { state.adjustment = adjustment; state.healthNote = healthNote; }));
    }
    if (notes !== (state.notes || '')) {
      reqs.push(jsonFetch(C.urls.notes, {
        method: 'POST', body: JSON.stringify({ notes }),
      }).then(() => { state.notes = notes; }));
    }
    // The guide-check route is a flip, not a set, so it is only called on a real change.
    if (testToggle && testToggle.checked !== state.testEnabled) {
      reqs.push(jsonFetch(`/api/channel-tests/on-demand/${C.guideCheckJobId}/channels/${C.channelId}/toggle`, {
        method: 'POST',
      }).then(() => { state.testEnabled = testToggle.checked; }));
    }

    if (!reqs.length) { close(); return; }
    Promise.all(reqs)
      .then(() => {
        close();
        renderSettingsBar();
        showToast('Settings saved.');
        // The score, its offset line and the note marker are all server-rendered from
        // these values, so the page is reloaded rather than patched in three places.
        setTimeout(() => location.reload(), 500);
      })
      .catch(e => showToast(e.message || 'Could not save every setting - nothing was rolled back.', { type: 'error' }));
  }

  // ── Actions ───────────────────────────────────────────────────────────────

  function copyStreamUrl(btn) {
    const url = byId('stream-url-text').dataset.fullUrl;
    const done = () => {
      btn.textContent = 'Copied!';
      setTimeout(() => { btn.textContent = 'Copy'; }, 2000);
    };
    const fallback = () => {
      const ta = document.createElement('textarea');
      ta.value = url;
      ta.style.cssText = 'position:fixed;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      done();
    };
    // navigator.clipboard is undefined outside a secure context (plain http:// on a LAN
    // IP, not localhost) - calling .writeText on it throws synchronously and skips the
    // .catch() fallback entirely, so the button silently does nothing (dev/docs/BUGS.md
    // 2026-08-09). Guard existence first, same pattern as guide.js's modal-url-copy.
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(done).catch(fallback);
    } else {
      fallback();
    }
  }

  function repointChannel() {
    const r = C.repoint;
    if (!r) return;
    if (!confirm(`Re-point "${C.channelName}" to "${r.name}" on ${r.account}?\n\n` +
        'This transfers guide listing, channel-group assignment, SCHEDULED recordings, and ' +
        `channel-test enrollment to the surviving channel. "${C.channelName}" will no longer ` +
        'appear in the guide or be tested.')) return;
    jsonFetch(C.urls.repoint, {
      method: 'POST', body: JSON.stringify({ survivor_channel_id: r.id }),
    })
      .then(data => {
        showToast(data.message || 'Re-pointed successfully.');
        setTimeout(() => { location.href = `/channels/${r.id}`; }, 900);  // nav-ok: redirect after a save
      })
      .catch(e => showActionError(e.message || 'Re-point failed.'));
  }

  /* Delete this one channel, through the SAME preview-then-delete pair the Browse tab's
     sweep uses (dev/changelog/772). No confirm() of our own: the shared modal fetches the
     preview first, so it enumerates what goes and what is kept, and builds no delete
     button at all when this channel turns out not to be deletable - which makes the
     refusal and its reason the dialog rather than an error after a click. */
  function deleteChannel() {
    if (!C.isMissing) return;
    openMissingModal({
      title: 'Delete Channel',
      scopeText: '',
      emptyText: `"${C.channelName}" cannot be deleted right now.`,
      previewUrl: `${C.urls.missingPreview}?channel_ids=${C.channelId}`,
      deleteUrl: C.urls.missingDelete,
      deleteBody: { channel_ids: [C.channelId] },
      // This page is about to describe a row that no longer exists, so it cannot be the
      // thing that reloads.
      onDone: () => { location.href = C.urls.channelsHub; },  // nav-ok: redirect after deleting the channel
    });
  }

  /* Hide, un-hide, or hand the answer back to the rules. The button carries the value it
     posts rather than the JS deriving it, so the three states stay named in one place - the
     template - instead of being re-derived here from a flag that would have to mean two
     things at once. No confirm: hiding destroys nothing and this page stays reachable by id,
     which is where the un-hide lives. The reload is what re-renders the state bar, the
     kebab label and the deferral sentence together. */
  function setHideOverride(el) {
    const override = JSON.parse(el.dataset.override || 'null');
    jsonFetch(C.urls.hide, { method: 'POST', body: JSON.stringify({ override }) })
      .then((data) => {
        const st = data.hide || {};
        /* The un-hide case names the EPG gap: hiding deleted this channel's guide data and
           un-hiding does not fetch it back. The reloaded page states it permanently too;
           this is so the answer arrives with the action rather than only after the reload. */
        const visibleAgain = st.epg_gap
          ? 'Channel is visible again. Its guide data returns at the next sync.'
          : 'Channel is visible again.';
        showToast(st.label || (override === true ? 'Channel hidden.' : visibleAgain));
        location.reload();
      })
      .catch((e) => showActionError(e.message || 'Could not change this channel.'));
  }

  function testNow(btn) {
    btn.disabled = true;
    jsonFetch(C.urls.testNow, { method: 'POST' })
      .then(data => {
        showToast(data.message || 'Testing this channel now - the page will refresh when it finishes.');
        pollForTest(btn);
      })
      .catch(e => {
        btn.disabled = false;
        showActionError(e.message || 'Could not start the test.');
      });
  }

  // The test runs in a background thread, so the page watches the shared tester status
  // rather than holding the request open. Reloading when it goes idle is what puts the
  // new result on the page.
  function pollForTest(btn) {
    let sawRunning = false;
    const tick = () => {
      jsonFetch('/api/channel-tests/status')
        .then(s => {
          if (s.is_running) { sawRunning = true; setTimeout(tick, 2000); return; }
          if (!sawRunning) { setTimeout(tick, 2000); return; }
          location.reload();
        })
        .catch(() => { if (btn) btn.disabled = false; });
    };
    setTimeout(tick, 1500);
  }

  function openAddToGroupModal() {
    const body = document.createElement('div');
    body.innerHTML = '<p class="text-muted small">Loading groups&hellip;</p>';
    let groups = [];
    let picked = null;

    const draw = () => {
      const rows = groups.map(g =>
        `<label class="cd-grouprow"><input type="radio" name="cd-grp" value="${g.id}"` +
        `${picked === g.id ? ' checked' : ''}> ${escHtml(g.name)}` +
        `<span class="text-muted small">${g.member_count} channel${g.member_count === 1 ? '' : 's'}` +
        `${g.in_guide ? ' &middot; in guide' : ''}</span></label>`
      ).join('');
      body.innerHTML =
        '<p class="text-muted small">A channel group appears in the TV Guide as one row and records ' +
        'the best-scoring member, failing over to the next if the stream drops.</p>' +
        (rows || '<p class="text-muted small">You have no channel groups yet.</p>') +
        '<div class="gd-field full"><div class="gd-field-lbl">Or create a new group</div>' +
        '<input type="text" id="cd-new-group" class="form-control" placeholder="New group name" style="width:100%">' +
        '</div>';
    };

    body.addEventListener('change', (e) => {
      if (e.target.name === 'cd-grp') picked = Number(e.target.value);
    });

    const modal = buildModal({
      title: 'Add to channel group',
      body,
      footer: [
        { label: 'Cancel', class: 'btn' },
        { label: 'Add', class: 'btn btn-primary', onClick: (close) => { submit(close); return false; } },
      ],
    });

    function submit(close) {
      const newName = (body.querySelector('#cd-new-group') || {}).value || '';
      const req = newName.trim()
        ? jsonFetch('/api/channel-groups', {
            method: 'POST',
            body: JSON.stringify({ name: newName.trim(), channel_ids: [C.channelId] }),
          })
        : (picked
            ? jsonFetch(`/api/channel-groups/${picked}/members`, {
                method: 'POST', body: JSON.stringify({ channel_ids: [C.channelId] }),
              })
            : null);
      if (!req) { showToast('Pick a group or name a new one.', { type: 'error' }); return; }
      req.then(() => { close(); location.reload(); })
        .catch(e => showToast(e.message || 'Could not add the channel.', { type: 'error' }));
    }

    jsonFetch('/api/channel-groups')
      .then(data => { groups = data.groups || []; draw(); })
      .catch(() => { body.innerHTML = '<p class="text-muted small">Could not load your groups.</p>'; });
    return modal;
  }

  function openCreateCheck() {
    openCreateCheckModal({
      channelIds: [C.channelId],
      groupName: C.channelName,
      defaultName: `${C.channelName} - health check`,
      memberCount: 1,
      profiles: C.checkProfiles || [],
      profilesUrl: C.checkProfilesUrl,
      testerBusy: C.testerBusy,
      windowSettingsUrl: C.windowSettingsUrl,
      scheduleTemplateId: 'cc-schedule-fields',
      schedulePrefix: 'ccsched',
      // Deliberately NOT nameless: an ad hoc test of one channel has no group to derive a
      // job name from, so this is the one caller that still asks for one.
      allowAttachExisting: true,
      onDone: () => location.reload(),
    });
  }

  // ── Live preview ──────────────────────────────────────────────────────────
  // The server runs one ffmpeg that stream-copies the channel into a rolling HLS window
  // (app/preview.py); this plays it. The browser never sees the stream URL, only the
  // session's playlist. Every way this modal can go away stops the session: the Stop
  // button and the close/Esc/backdrop paths all run onClose, and leaving the page fires a
  // pagehide beacon. The server's idle reaper is the backstop for a tab that dies
  // without either, so a stop here is prompt rather than load-bearing.
  //
  // hls.js is loaded on the first click rather than with the page: it is the one third-
  // party script in the app (static/vendor/hls.js/NOTICE.md) and 386 KB nobody should pay
  // for on every channel page load.
  let hlsLoading = null;
  function loadHlsJs() {
    if (window.Hls) return Promise.resolve(window.Hls);
    if (!hlsLoading) {
      hlsLoading = new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = C.urls.hlsJs;
        s.onload = () => resolve(window.Hls);
        s.onerror = () => { hlsLoading = null; reject(new Error('Could not load the video player script.')); };
        document.head.appendChild(s);
      });
    }
    return hlsLoading;
  }

  function openPreview() {
    const stage = document.createElement('div');
    stage.innerHTML = `
      <div class="cd-preview-stage">
        <video class="cd-preview-video" playsinline controls></video>
        <button type="button" class="btn btn-primary cd-preview-play" hidden>Play</button>
      </div>
      <div class="cd-preview-status" role="status">Starting…</div>`;
    const video = stage.querySelector('video');
    const playBtn = stage.querySelector('.cd-preview-play');
    const statusEl = stage.querySelector('.cd-preview-status');

    let session = null;     // the start response: status_url / playlist_url / stop_url
    let hls = null;
    let pollTimer = null;
    let closed = false;
    let recoveredOnce = false;

    const setStatus = (text, kind = '') => {
      statusEl.textContent = text;
      statusEl.dataset.kind = kind;
    };

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
      video.pause();
      video.removeAttribute('src');
      video.load();
    };

    const modal = buildModal({
      title: `Preview - ${C.channelName}`,
      body: stage,
      panelClass: 'modal-wide cd-preview-modal',
      footer: [{ label: '■ Stop', class: 'btn', onClick: (close) => close() }],
      onClose: () => {
        closed = true;
        window.removeEventListener('pagehide', onPageHide);
        detachPlayer();
        stopSession();
      },
    });
    window.addEventListener('pagehide', onPageHide);

    // The session ended on the server. Name why - a preview that stops without saying is
    // a number nobody can explain - and leave the modal up so the reason can be read.
    const ended = (s) => {
      detachPlayer();
      session = null;
      const why = [s.reason_text, s.detail].filter(Boolean).join('\n');
      setStatus(why || 'Stopped.', s.reason === 'user' ? '' : 'error');
    };

    const tryPlay = () => {
      const p = video.play();
      if (!p || !p.catch) return;
      p.then(() => { playBtn.hidden = true; })
       .catch((err) => {
         if (err && err.name === 'NotAllowedError') {
           playBtn.hidden = false;
           setStatus('Ready - press Play. The browser would not start sound on its own.');
         }
       });
    };
    playBtn.addEventListener('click', tryPlay);
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

    // Prefer hls.js wherever Media Source Extensions exist (desktop Chrome/Firefox/Edge,
    // Android Chrome), fall back to the browser's own HLS where it is the only option
    // (iOS Safari), and say so where there is neither.
    const attach = () => {
      const url = session.playlist_url;
      const nativeHls = video.canPlayType('application/vnd.apple.mpegurl');
      const useNative = () => {
        video.src = url;
        video.addEventListener('loadedmetadata', tryPlay, { once: true });
      };
      loadHlsJs()
        .then((Hls) => {
          if (Hls && Hls.isSupported()) {
            hls = new Hls({ liveSyncDurationCount: 3, liveMaxLatencyDurationCount: 8 });
            hls.on(Hls.Events.MANIFEST_PARSED, tryPlay);
            hls.on(Hls.Events.ERROR, onHlsError);
            hls.loadSource(url);
            hls.attachMedia(video);
          } else if (nativeHls) {
            useNative();
          } else {
            setStatus('This browser cannot play HLS video, so the preview cannot show here.', 'error');
          }
        })
        .catch((e) => {
          if (nativeHls) useNative();
          else setStatus(e.message || 'Could not load the video player script.', 'error');
        });
    };

    // hls.js gives a missing manifest very few retries, so the playlist is only handed to
    // it once the server says the first segment exists; the poll keeps running afterwards
    // so a stop decided on the server (preempted, idle, ended) reaches the modal by name.
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

    jsonFetch(C.urls.preview, { method: 'POST' })
      .then((data) => {
        if (closed) {
          // Closed before the start round-trip came back: stop what we just started.
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

  // The sticky bottom bar's kebab (DESIGN.md 9.6: a dropdown opening upward out of a fixed
  // bar clips). Its items are READ FROM #cd-kebab rather than re-listed here, so the two
  // surfaces cannot offer different actions - the server-rendered menu stays the one source.
  function openActionsSheet() {
    const src = byId('cd-kebab');
    if (!src) return;
    // .cd-sheet-row, not guide.css's .guide-sheet-row: guide.css is only loaded when this
    // channel has EPG data, and these actions exist on every channel.
    const rows = Array.from(src.querySelectorAll('.menu-item')).map(b =>
      `<button type="button" class="cd-sheet-row" data-sheet-act="${b.dataset.act}">${b.innerHTML}</button>`
    ).join('');
    // buildModal already renders as a bottom sheet at phone widths (style.css 9.6), and the
    // bar that opens this is phone-only, so no panel variant is needed.
    const sheet = buildModal({ title: C.channelName, body: rows });
    sheet.addEventListener('click', (e) => {
      const row = e.target.closest('[data-sheet-act]');
      if (!row) return;
      // Closed before dispatching: several of these open a modal of their own, and stacking
      // one on top of the sheet leaves two overlays and two backdrops.
      sheet.closeModal();
      pageAction(row.dataset.sheetAct, row);
    });
  }

  /* Roll the health score back by hand - a full reset, or one observation at a time.
     Both open a confirm naming exactly what will happen and what the score will become,
     because the server has already computed both (C.healthRollback, from the same replay
     that will run). Never a bare confirm(): the reset also clears the manual offset, and a
     dialog that does not say so is the "UI text describing backend behavior" defect. */
  function rollbackHealth(action) {
    const rb = C.healthRollback || {};
    if (!rb.available) {
      showToast('This channel has no observations left to unwind.', { type: 'error' });
      return;
    }
    const next = rb.next || {};
    const scoreWord = (v) => (v === null || v === undefined ? 'no score' : String(v));

    let title, confirmLabel, body;
    if (action === 'reset') {
      body = `<p>All ${rb.available} observation${rb.available === 1 ? '' : 's'} behind this `
           + `channel's health score will stop counting, and the score goes from `
           + `<strong>${scoreWord(rb.current_score)}</strong> to <strong>no score at all</strong> - `
           + 'as if the channel had never been tested.</p>';
      if (rb.manual_adjustment) {
        body += `<p><strong>The manual adjustment of ${rb.manual_adjustment > 0 ? '+' : ''}`
              + `${rb.manual_adjustment} will be cleared too</strong>, along with its note. `
              + 'A hand-set offset on a channel with no observations behind it is a number '
              + 'nothing can explain.</p>';
      }
      body += '<p>Nothing is deleted: every test, recording and screenshot stays on record and '
            + 'stays visible in the Activity Timeline, marked as no longer counted. The next '
            + 'health check or recording starts the score over from scratch.</p>';
      title = 'Reset health score';
      confirmLabel = 'Reset score';
    } else {
      body = `<p>The most recent observation - ${escHtml(next.label || 'the newest one')} - `
           + 'will stop counting toward this channel\'s health score.</p>'
           + `<p>The score goes from <strong>${scoreWord(rb.current_score)}</strong> to `
           + `<strong>${scoreWord(next.score_after)}</strong>, over `
           + `${next.observations_after} remaining observation`
           + `${next.observations_after === 1 ? '' : 's'}. `
           + `${rb.available - 1} further step-back${rb.available - 1 === 1 ? '' : 's'} `
           + 'would then be available.</p>'
           + '<p>Nothing is deleted - the observation stays on the Activity Timeline, marked '
           + 'as no longer counted.</p>';
      /* Observations the stored score counted but nothing can replay - a health check old
         enough that retention has since deleted its row. Named rather than absorbed: their
         residual leaves with them, so the projected score above is honest only if the user
         knows what it was computed over. */
      if (rb.unledgered) {
        body += `<p class="text-muted small">${rb.unledgered} even older observation`
              + `${rb.unledgered === 1 ? '' : 's'} behind the current score can no longer be `
              + 'replayed - their test records have been deleted by retention - so their '
              + 'small remaining influence is dropped along with this step back.</p>';
      }
      title = 'Step back one observation';
      confirmLabel = 'Step back';
    }

    const el = document.createElement('div');
    el.innerHTML = body;
    buildModal({
      title,
      body: el,
      footer: [
        { label: 'Cancel', class: 'btn' },
        {
          label: confirmLabel,
          class: action === 'reset' ? 'btn btn-danger' : 'btn btn-primary',
          onClick: (close) => {
            jsonFetch(C.urls.healthRollback, {
              method: 'POST', body: JSON.stringify({ action }),
            }).then((data) => {
              close();
              /* The reload is what re-renders the score, the kebab's remaining count and
                 the timeline's excluded markers together - three regions one updater. */
              sessionStorage.setItem('cd-rollback-toast', (data.result || {}).detail || 'Health score updated.');
              location.reload();
            }).catch(e => showToast(e.message || 'Could not roll the score back.', { type: 'error' }));
            return false;
          },
        },
      ],
    });
  }

  /* The action's own sentence, carried across the reload it triggers. */
  (function showPendingRollbackToast() {
    const pending = sessionStorage.getItem('cd-rollback-toast');
    if (!pending) return;
    sessionStorage.removeItem('cd-rollback-toast');
    showToast(pending);
  })();

  function pageAction(act, el) {
    switch (act) {
      case 'settings': openSettingsModal(el && el.dataset.focus); return;
      case 'actions-sheet': openActionsSheet(); return;
      case 'sections': sectionLayout.open(); return;
      case 'copy-url': copyStreamUrl(el); return;
      case 'repoint': repointChannel(); return;
      case 'test-now': testNow(el); return;
      case 'preview': openPreview(); return;
      case 'add-group': openAddToGroupModal(); return;
      case 'create-check': openCreateCheck(); return;
      case 'delete-channel': deleteChannel(); return;
      case 'hide-channel': setHideOverride(el); return;
      case 'health-reset': rollbackHealth('reset'); return;
      case 'health-step-back': rollbackHealth('step_back'); return;
      case 'jump': {
        const target = document.querySelector(`[data-section="${el.dataset.jumpSection}"]`);
        if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
      }
      default: return;   // not one of this page's actions
    }
  }

  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-act]');
    if (!el) return;
    e.preventDefault();
    pageAction(el.dataset.act, el);
  });

  renderSettingsBar();
})();
