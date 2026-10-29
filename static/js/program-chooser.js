/* The program chooser: what a click on a TV Guide program opens (DESIGN.md 12.10 / 13.10,
   dev/changelog/1321, approved as mockup 45).

   The program in full, then its actions as icon tiles with the recording action first. A
   desktop gets util.js's side panel, so the grid stays live beside it and a click on another
   program refills it; a phone gets util.js's bottom sheet with the program at its top. Every
   action is something that already exists, called as it is: the record dialog
   (guide.js::openModal, in all of its modes), the channel page's Test now route, the live
   preview (live-preview.js), the recording page's player (recording-player.js), the airing
   search and the channel and group pages.

   A group row's Test now and Preview act on the channel the row records from now
   (`active_channel_id`), the one the row shows as serving, and say so.

   Loaded after guide.js by both pages that draw a guide grid - the TV Guide and the channel
   page's What's on card - whose two cell click sites call openProgramChooser(). */

(() => {
  const REC = statusVocab('REC_STATUS');

  // Every status named and none left to a default: FAILED and ABORTED never reach the guide
  // (app/routes/guide.py leaves them out so Record stays offered), and read as no recording
  // here for the same reason.
  function recState(prog) {
    if (!prog.has_recording || !prog.recording_id) return 'none';
    switch (prog.recording_status) {
      case REC.SCHEDULED: return 'scheduled';
      case REC.IN_PROGRESS:
      case REC.PAUSED:
      case REC.RETRYING: return 'capturing';
      case REC.CONCATENATING:
      case REC.ANALYZING:
      case REC.CONVERTING:
      case REC.COMPLETED: return 'captured';
      case REC.FAILED:
      case REC.ABORTED: return 'none';
      default:
        console.error('recording status the program chooser does not name:', prog.recording_status);
        return 'none';
    }
  }

  function airState(prog) {
    const now = Date.now();
    if (utcIsoToDate(prog.stop_time).getTime() <= now) return 'ended';
    if (utcIsoToDate(prog.start_time).getTime() <= now) return 'airing';
    return 'upcoming';
  }

  // The channel Test now and Preview act on.
  function target(ch) {
    return ch.is_group
      ? { id: ch.active_channel_id, name: ch.active_channel_name }
      : { id: ch.id, name: ch.name };
  }

  function airingsUrl(prog) {
    const base = GUIDE_CONFIG.programAiringsUrl;
    if (!base || prog.is_dummy) return null;
    return `${base}${base.includes('?') ? '&' : '?'}q=${encodeURIComponent(prog.title)}`;
  }

  function chooserActions(prog, ch) {
    const rs = recState(prog);
    const air = airState(prog);
    const t = target(ch);
    const acts = [];

    if (rs === 'capturing') {
      acts.push({ act: 'record', icon: 'rec-controls', label: 'Recording controls',
        tip: 'Change the end time, or stop, pause or abort the recording' });
      acts.push({ act: 'play', icon: 'play', label: 'Play',
        tip: 'Watch it while it records, a little behind live' });
    } else if (rs === 'scheduled') {
      acts.push({ act: 'record', icon: 'edit-recording', label: 'Edit recording',
        tip: 'Change its times or profile, or cancel it' });
    } else if (air === 'ended') {
      acts.push({ act: 'record', icon: 'record', label: 'Schedule recording', disabled: true });
    } else if (air === 'airing') {
      acts.push({ act: 'record', icon: 'record', label: 'Record now',
        tip: 'Starts now. The part that already aired will not be in the file' });
    } else {
      acts.push({ act: 'record', icon: 'record', label: 'Schedule recording',
        tip: 'Pick a profile and padding, then schedule it' });
    }

    acts.push({ act: 'test', icon: 'test', label: 'Test now',
      tip: ch.is_group ? `Tests ${t.name}, the channel this group records from now`
                       : 'A short capture and probe: does it play right now?' });
    acts.push({ act: 'preview', icon: 'preview', label: 'Preview',
      tip: air === 'airing' ? `Plays ${t.name} live`
                            : `Plays ${t.name} live - what is on now, not this program` });
    acts.push({ act: 'copy', icon: 'copy', label: 'Copy stream URL',
      tip: 'The full address, account login included, for VLC or another player' });
    if (rs !== 'none') {
      acts.push({ icon: 'recordings', label: 'Recording page',
        href: GUIDE_CONFIG.recDetailUrlBase + prog.recording_id });
    }
    const airings = airingsUrl(prog);
    if (airings) {
      acts.push({ icon: 'airings', label: 'Other airings', href: airings,
        tip: `Every showing of "${prog.title}" on every channel` });
    }
    // Either card is already on that page: the channel page's What's On, and the group
    // page's, whose every cell records from the group.
    if ((GUIDE_CONFIG.embed || 'page') === 'page') {
      acts.push(ch.is_group
        ? { icon: 'group', label: 'Channel group page', href: GUIDE_CONFIG.groupDetailUrlBase + ch.group_id }
        : { icon: 'channel', label: 'Channel page', href: GUIDE_CONFIG.chDetailUrlBase + ch.id });
    }
    return acts;
  }

  // Why the record tile is greyed, under the tiles: a tile has no room to say it.
  function noteFor(prog, acts) {
    if (!acts.some((a) => a.act === 'record' && a.disabled)) return '';
    return airingsUrl(prog)
      ? 'This program has ended. Try Other airings for another showing.'
      : 'This program has ended, so it cannot be recorded.';
  }

  function headHtml(prog, ch) {
    const air = airState(prog);
    const airWord = air === 'ended' ? '<span class="chooser-air">Ended</span>'
      : air === 'airing' ? '<span class="chooser-air is-on">On now</span>' : '';
    const chip = prog.has_recording
      ? recStatusBadge(prog.recording_status, prog.recording_partial, prog.recording_stopped,
                       recStarting(prog.recording_status, prog.recording_start_time), Infinity)
      : '';
    const state = chip || airWord ? `<div class="chooser-state">${chip}${airWord}</div>` : '';
    // A filler slot on a channel with no listings has no program to describe.
    const fmtT = (iso) => fmtTimeTz(utcIsoToDate(iso));
    const details = progHeadHtml(prog, { withTitle: false })
      || sheetLine('When', `${fmtT(prog.start_time)} - ${fmtT(prog.stop_time)}`)
        + sheetLine('Channel', prog.channel_name);
    const serving = ch.is_group
      ? `<p class="chooser-note">Test now and Preview use ${escHtml(ch.active_channel_name)}, `
        + 'the channel this group records from now.</p>'
      : '';
    return `${state}<div class="prog-head">${details}</div>${serving}`;
  }

  // ── Running an action ─────────────────────────────────────────────────────

  // What the chooser is showing: {prog, ch} while the side panel is open. The phone's sheet
  // is a dialog, which holds the guide's refresh by itself, so it is never tracked here.
  let current = null;

  function closeChooser() {
    if (current) closeSidePanel();
  }

  function startTest(t, btn) {
    if (btn) btn.disabled = true;
    jsonFetch(`/api/channels/${encodeURIComponent(t.id)}/test-now`, { method: 'POST' })
      .then((data) => {
        // The channel page refreshes itself when the result lands; the guide does not.
        showToast(GUIDE_CONFIG.embed === 'channel'
          ? (data.message || `Testing "${t.name}" now.`)
          : `Testing "${t.name}" now. Its channel page shows the result when the test finishes.`);
      })
      .catch((e) => showToast(e.message || 'Could not start the test.', { type: 'error' }))
      .finally(() => { if (btn) btn.disabled = false; });
  }

  function runAction(act, prog, ch, btn) {
    switch (act) {
      case 'record':
        // Replaces the chooser rather than opening over it (mockup 45's pick,
        // dev/changelog/1318): the dialog opens alone.
        closeChooser();
        openModal(prog, ch);
        return;
      case 'play':
        closeChooser();
        window.recordingDialogs.play(prog.recording_id, prog.title);
        return;
      case 'test':
        startTest(target(ch), btn);
        return;
      case 'preview': {
        const t = target(ch);
        closeChooser();
        openLivePreview({ channelId: t.id, channelName: t.name });
        return;
      }
      case 'copy':
        // Unmasked, as the record dialog's own Copy is: copying it is the user's deliberate
        // act. The toast says what was copied and never shows the URL itself.
        copyText(prog.stream_url)
          .then(() => showToast('Stream URL copied. It includes the account login.'))
          .catch((e) => showToast(e.message, { type: 'error' }));
        return;
      default:
        console.error('program chooser action with no handler:', act);
    }
  }

  // ── The two forms ─────────────────────────────────────────────────────────

  function openPanel(prog, ch) {
    const acts = chooserActions(prog, ch);
    const note = noteFor(prog, acts);
    const body = document.createElement('div');
    body.innerHTML = headHtml(prog, ch) + actionTilesHtml(acts)
      + (note ? `<p class="act-tiles-note">${escHtml(note)}</p>` : '');
    body.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn || btn.disabled) return;
      runAction(btn.dataset.act, prog, ch, btn);
    });
    openSidePanel({ title: prog.title, body, onClose: () => { current = null; } });
    current = { prog, ch };
  }

  function openSheet(prog, ch) {
    const acts = chooserActions(prog, ch);
    openMenuSheet(acts, {
      title: prog.title,
      tiles: true,
      head: headHtml(prog, ch),
      note: noteFor(prog, acts),
      onAct: (act) => runAction(act, prog, ch, null),
    });
  }

  function openProgramChooser(prog, ch) {
    if (isPhone()) {
      closeChooser();
      openSheet(prog, ch);
    } else {
      openPanel(prog, ch);
    }
  }

  // The grid re-read its data under an open panel: show the same program as it is now. A
  // program the new data no longer carries keeps what it showed.
  document.addEventListener('guide:data', () => {
    if (!current) return;
    const row = channelData.find((c) => String(c.id) === String(current.ch.id));
    const prog = row && (row.programs || []).find((p) => p.id === current.prog.id
      && p.start_time === current.prog.start_time);
    if (prog) openPanel(prog, row);
  });

  // A panel is the desktop's form; crossing into the phone layout puts it away.
  MOBILE_MQ.addEventListener('change', () => { if (MOBILE_MQ.matches) closeChooser(); });

  window.openProgramChooser = openProgramChooser;
})();
