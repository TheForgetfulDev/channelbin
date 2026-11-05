/* Restarting the service, from any [data-restart-service] button: the restart-required banner
   on every page (templates/base.html) and the Maintenance page's Service control card. Loaded
   by base.html, so one flow answers both and they cannot come to behave differently.

   The server refuses (409) while a recording is capturing, joining or converting, and returns
   the blocking rows so this can name them and offer to restart anyway. */
(() => {
  'use strict';

  const toast = (msg, isError) =>
    showToast(msg, { type: isError ? 'error' : 'success', durationMs: 2500 });

  // A full-service restart makes the button label alone too easy to miss, so this
  // stays up (non-dismissable - nothing else on the page works mid-restart anyway)
  // until the service is genuinely confirmed back, then reloads.
  //
  // "Back" is proven by `/api/settings/restart-status` reporting an instance id other
  // than the one the restart POST answered with - the id is minted once per process.
  // Waiting to catch a failed poll followed by a good one is only the fallback for
  // when the POST's own connection dropped before it answered: a container restart
  // finishes inside one poll interval, so no poll ever fails and a modal keyed on
  // that alone waits forever on a healthy app (dev/changelog/1022).
  const RESTART_POLL_MS = 1500;
  const RESTART_SLOW_AFTER_MS = 45000;

  const showRestartWaitModal = (oldInstanceId) => {
    const body = document.createElement('div');
    body.className = 'restart-wait';
    body.innerHTML = '<div class="spinner mnt-spinner"></div>' +
      '<div class="restart-wait-status">Waiting for the service to come back online...</div>';
    const statusEl = body.querySelector('.restart-wait-status');
    const modal = buildModal({ title: 'Restarting ChannelBin', body, dismissable: false });

    const startedAt = Date.now();
    // The old process is still alive for a moment after the request that triggered
    // this, so an early poll succeeding does NOT mean it's back. With no id to compare
    // against, only a poll that fails and THEN succeeds again does.
    let sawDrop = false;
    let slowWarned = false;
    let stopped = false;

    const waitingText = () =>
      `Waiting for the service to come back online... (${Math.round((Date.now() - startedAt) / 1000)}s)`;

    const addReloadButton = () => {
      const warn = document.createElement('div');
      warn.className = 'restart-wait-warn';
      warn.textContent = 'This is taking longer than usual. Still checking - you can reload manually if you like.';
      body.appendChild(warn);
      const foot = document.createElement('div');
      foot.className = 'modal-foot';
      const btn = document.createElement('button');
      btn.className = 'btn';
      btn.textContent = 'Reload page';
      btn.addEventListener('click', () => window.location.reload());
      foot.appendChild(btn);
      modal.querySelector('.modal-panel').appendChild(foot);
    };

    const isBack = (data) => {
      const id = data && data.instance_id;
      if (oldInstanceId && id) return id !== oldInstanceId;
      return sawDrop;
    };

    const tick = () => {
      if (stopped) return;
      jsonFetch('/api/settings/restart-status')
        .then((data) => {
          if (stopped) return;
          if (isBack(data)) {
            stopped = true;
            statusEl.textContent = 'Back online - reloading...';
            setTimeout(() => window.location.reload(), 500);
            return;
          }
          waiting();
        })
        .catch(() => {
          if (stopped) return;
          sawDrop = true;
          waiting();
        });
    };

    // Every tick that is not the reload, success or failure alike, so the timer and
    // the slow-restart escape hatch never depend on catching the connection down.
    const waiting = () => {
      statusEl.textContent = waitingText();
      if (!slowWarned && Date.now() - startedAt >= RESTART_SLOW_AFTER_MS) {
        slowWarned = true;
        addReloadButton();
      }
      setTimeout(tick, RESTART_POLL_MS);
    };

    tick();
  };

  // A parked recording (waiting on another one to finish with the machine) never blocks a
  // restart, but a parked conversion holds a suspended encode that a restart throws away,
  // so every restart surface names it - in the same words tools/check_busy.py prints
  // (dev/changelog/1026). Empty string when there is nothing parked.
  const parkedSection = (parked) => (parked && parked.length
    ? `<p class="text-muted">Parked, waiting on another recording - these do not stop a
       restart:</p>
       <ul class="blocking-list">${parked.map((r) => `<li>${escHtml(r.label)}</li>`).join('')}</ul>`
    : '');

  const doRestart = (btn, label, force) => {
    btn.disabled = true;
    btn.textContent = 'Restarting...';
    const resetBtn = () => {
      btn.disabled = false;
      btn.textContent = label;
    };
    return jsonFetch('/api/settings/restart', {
      method: 'POST',
      body: JSON.stringify({ force }),
    })
      .then((data) => showRestartWaitModal(data && data.instance_id))
      .catch((e) => {
        const blocking = e.data && e.data.blocking;
        if (e.status === 409 && blocking && blocking.length && !force) {
          // The whole point of the 409 is that the server knows WHAT is in flight.
          // Naming each row is what makes "restart anyway" an informed choice
          // rather than a shrug, so the list is rendered rather than summarized.
          // Every way out that is not "restart anyway" - Cancel, Escape, the
          // backdrop, the x - has to put the button back, so the reset hangs off
          // onClose with a flag rather than off the Cancel handler alone.
          let proceeded = false;
          buildModal({
            title: 'Work is in flight',
            body: `<p>Restarting kills it. A recording loses whatever has not been written yet;
                   a conversion restarts from the beginning on the next attempt; a search index
                   rebuild has to start over, and search runs slower until it finishes; a health
                   check or channel test loses the channels it has not reached; an account sync
                   fetches its playlist and guide data again from the start.</p>
                   <ul class="blocking-list">${blocking.map((r) =>
                     `<li>${escHtml(r.label)}</li>`).join('')}</ul>
                   ${parkedSection(e.data.parked)}`,
            footer: [
              { label: 'Cancel', class: 'btn' },
              {
                label: 'Restart anyway',
                class: 'btn btn-danger',
                onClick: (close) => { proceeded = true; close(); doRestart(btn, label, true); },
              },
            ],
            onClose: () => {
              if (proceeded) return;
              toast('Restart cancelled.');
              resetBtn();
            },
          });
          return undefined;
        }
        // A dropped connection here is the restart working; a real error carries a status.
        if (e.status) {
          toast(`Restart failed: ${e.message || 'unknown'}`, true);
          resetBtn();
        } else {
          showRestartWaitModal();
        }
        return undefined;
      });
  };

  const openRestartConfirm = (btn, parkedHtml) => {
    const label = btn.textContent;
    buildModal({
      title: 'Restart the service?',
      body: `<p>Restart the ChannelBin service now? Active page connections will briefly drop.</p>
             ${btn.dataset.docker ? `<p class="restart-wait-warn">The container comes back only if
             it has a restart policy set (for example <code>restart: unless-stopped</code>).
             Without one, it stops and stays down.</p>` : ''}
             <p class="text-muted">The server refuses while a capture, join, conversion,
             search index rebuild, health check, channel test or account sync is running, and
             will say what is blocking it.</p>
             ${parkedHtml}`,
      footer: [
        { label: 'Cancel', class: 'btn' },
        {
          label: 'Restart now',
          class: 'btn btn-danger',
          onClick: (close) => { close(); doRestart(btn, label, false); },
        },
      ],
    });
  };

  // Delegated: the banner's button is written in after a save (base.html's
  // checkRestartStatus), so one bound at load would miss it.
  //
  // Parked rows are asked for before the confirm opens because the POST never refuses over
  // them: this dialog is the only place a restart with just a parked conversion in flight
  // can say what it will cost. A failed lookup says so rather than implying nothing is parked.
  document.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-restart-service]');
    if (!btn) return;
    jsonFetch('/api/settings/restart-parked')
      .then((data) => openRestartConfirm(btn, parkedSection(data && data.parked)))
      .catch(() => openRestartConfirm(btn, `<p class="restart-wait-warn">Could not check for
        parked conversions, so a restart may discard one that is part-way done.</p>`));
  });
})();
