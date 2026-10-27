/* The recording page's two dialogs for a running group recording (dev/changelog/1254):
   Switch channel - move the recording to a channel the user picks, and ask about the one
   left - and Block accounts - the record modal's own picker, reached from the page, saying
   before it saves what the block will do to the capture.

   Loaded after guide.js, whose loadBlockPicker/blockPickerPayload draw the picker's rows:
   one picker, two wrappers. The page's delegated click handler hands `switch` and `block`
   here through window.recordingDialogs. */

(() => {
  const esc = escHtml;
  const band = (s) => (s == null ? 'hb-none' : s >= 90 ? 'hb-great' : s >= 80 ? 'hb-good' : s >= 50 ? 'hb-fair' : 'hb-poor');

  function acctPill(a) {
    return `<span class="ch-pill acct-pill"><span class="acct-dot" style="background: ${esc(a.color || 'var(--text-faint)')}"></span>${esc(a.name)}</span>`;
  }

  function choiceRow(c) {
    const score = c.score == null
      ? '<span class="h-mini hb-none tip-plain" data-tip="Never tested.">-</span>'
      : `<span class="h-mini ${band(c.score)} tip-plain" data-tip="Lifetime health score ${c.score} of 100.">${c.score}</span>`;
    return `<label class="fp-radio${c.ok ? '' : ' sw-off'}">
      <div class="sw-row"><input type="radio" name="sw-member" value="${c.channel_id}"${c.ok ? '' : ' disabled'}>
        <span class="sw-name">${esc(c.name)}</span>${acctPill(c.account)}${score}</div>
      <span class="sw-why${c.warn ? ' warn' : ''}">${esc(c.why)}</span>
    </label>`;
  }

  function leaveHtml(current, group) {
    return `<div class="sw-q">What about "${esc(current.name)}", the channel you are leaving?</div>
      <label class="fp-radio"><input type="radio" name="sw-leave" value="keep" checked> Leave it as it is
        <span class="text-muted">It stays in the group and can be picked again - by you or by a failover.</span></label>
      <label class="fp-radio"><input type="radio" name="sw-leave" value="off"> Turn its Recording switch off in "${esc(group)}"
        <span class="text-muted">Nothing records from it until you turn it back on from the group page. Health checks go on.</span></label>
      <label class="fp-radio"><input type="radio" name="sw-leave" value="block"> Block "${esc(current.account)}" for the rest of this recording
        <span class="text-muted">Nothing in ChannelBin opens a stream on the account while this recording can still record - so a TV can watch live on it.</span></label>`;
  }

  // What a move said, as one toast: the recording's own event sentence, then the
  // leave-behind outcome. A move the watchdog had not confirmed within the wait says so.
  function reportMove(r, fallback) {
    if (r.moved === null) { showToast(r.message, { type: 'info', durationMs: 9000 }); return; }
    const said = [r.message || fallback, r.left].filter(Boolean).join(' ');
    showToast(said, { type: r.moved ? 'success' : 'warning', durationMs: 10000 });
  }

  async function openSwitch(rid) {
    let data;
    try {
      data = await jsonFetch(`/api/recordings/${rid}/switch-options`);
    } catch (e) {
      showToast(e.message, { type: 'error' });
      return;
    }
    const cur = data.current || { name: '?', account: '?' };
    const since = data.since ? ` since ${fmtTimeTz(new Date(data.since + 'Z'))}` : '';
    const body = `<p class="sw-from">Recording from <b>${esc(cur.name)}</b> (${esc(cur.account)})${since}. Pick the channel to continue from; the recording carries on without a gap you would notice.</p>
      <div id="sw-error" class="notice notice-warn" style="display:none"></div>
      <div class="sw-list">${data.choices.map(choiceRow).join('')}</div>
      ${leaveHtml(cur, data.group)}`;
    let busy = false;
    const modal = buildModal({
      title: 'Switch channel',
      body,
      panelClass: 'modal-wide',
      footer: [
        { label: 'Cancel', onClick: (close) => close() },
        { label: 'Switch', class: 'btn btn-primary', onClick: (close) => {
          if (busy) return false;
          const picked = modal.querySelector('input[name="sw-member"]:checked');
          const err = modal.querySelector('#sw-error');
          if (!picked) { err.textContent = 'Pick the channel to switch to.'; err.style.display = ''; return false; }
          const leave = modal.querySelector('input[name="sw-leave"]:checked').value;
          busy = true;
          err.style.display = 'none';
          jsonFetch(`/api/recordings/${rid}/switch-channel`, {
            method: 'POST', body: JSON.stringify({ channel_id: Number(picked.value), leave }),
          }).then((r) => {
            close();
            reportMove(r, 'Switched.');
            // The header, stats and event log are swapped from the server on the page's
            // own timer; the chip and the log should follow the move without the wait.
            if (window.__refreshRecordingPage) window.__refreshRecordingPage();
          }).catch((e) => {
            busy = false;
            err.textContent = e.message;
            err.style.display = '';
          });
          return false;
        } },
      ],
    });
  }

  async function openBlock(rid) {
    const prefix = 'rec-blk';
    // A <label> heading, not .sec-title: that class is a phone-only sub-region title and is
    // display:none on the desktop.
    const body = `<div class="acct-block-pick form-group" id="${prefix}-blocks" style="display:none">
        <label>Block account use during this recording</label>
        <span class="form-hint">Nothing in ChannelBin opens a stream on a blocked account while this
          recording can still record - so a TV can watch live on it. A channel group records from
          members on other accounts; with nowhere else to go, the recording waits for the block to end
          and records what is left.</span>
        <input type="hidden" id="${prefix}-blocks-loaded" value="">
        <span id="${prefix}-blocks-count" hidden></span>
        <div id="${prefix}-blocks-list"></div>
      </div>
      <div id="${prefix}-answer" class="notice notice-info" style="display:none"></div>`;
    let busy = false;
    const modal = buildModal({
      title: 'Block accounts',
      body,
      footer: [
        { label: 'Cancel', onClick: (close) => close() },
        { label: 'Save', class: 'btn btn-primary', onClick: (close) => {
          if (busy) return false;
          const blocks = blockPickerPayload(prefix);
          if (!blocks) { showToast('The account list did not load; nothing was changed.', { type: 'error' }); return false; }
          busy = true;
          jsonFetch(`/api/recordings/${rid}/account-blocks`, {
            method: 'POST', body: JSON.stringify({ blocks }),
          }).then((r) => {
            close();
            if (r.moved === undefined) showToast(r.message);
            else reportMove(r, r.message);
            if (window.__refreshRecordingPage) window.__refreshRecordingPage();
          }).catch((e) => {
            busy = false;
            showToast(e.message, { type: 'error' });
          });
          return false;
        } },
      ],
    });
    await loadBlockPicker(prefix, rid);
    const saveBtn = Array.from(modal.querySelectorAll('.modal-foot .btn')).find((b) => /^(Save|Block anyway)$/.test(b.textContent));
    const answer = modal.querySelector(`#${prefix}-answer`);
    let token = 0;
    // Before it is saved, say what the block will do to the capture: which channel the
    // recording moves to, or that it has nowhere to go and stays put.
    const refresh = async () => {
      const blocks = blockPickerPayload(prefix) || [];
      const mine = ++token;
      let r;
      try {
        r = await jsonFetch(`/api/recordings/${rid}/block-preview?blocks=${encodeURIComponent(JSON.stringify(blocks))}`);
      } catch (e) {
        return;
      }
      if (mine !== token) return;
      if (!r.affects) { answer.style.display = 'none'; saveBtn.textContent = 'Save'; return; }
      let text;
      let ok;
      if (r.to && !r.held) {
        ok = true;
        text = `This recording is on "${r.current.name}" (${r.current.account}). Blocking the account moves it to "${r.to.name}" (${r.to.account}, score ${r.to.score}) as soon as you save. Nothing is scored against "${r.current.name}".`;
      } else if (r.to && r.held) {
        ok = false;
        text = `This recording is on "${r.current.name}" (${r.current.account}) and no other channel can take over right now: "${r.to.name}" is next, but "${r.to.account}" is at its connection limit. The recording stays on "${r.current.name}" until the block ends or a slot frees, and says so on this page. Block anyway?`;
      } else {
        ok = false;
        text = `This recording is on "${r.current.name}" (${r.current.account}) and no other channel in the group can take over: each is on a blocked account, has Recording off, or already failed this recording. The recording stays on "${r.current.name}" until the block ends, and says so on this page. Block anyway?`;
      }
      answer.className = `notice ${ok ? 'notice-info' : 'notice-warn'}`;
      answer.textContent = text;
      answer.style.display = '';
      saveBtn.textContent = ok ? 'Save' : 'Block anyway';
    };
    modal.addEventListener('change', refresh);
    refresh();
  }

  window.recordingDialogs = Object.assign(window.recordingDialogs || {},
    { switch: openSwitch, block: openBlock });
})();
