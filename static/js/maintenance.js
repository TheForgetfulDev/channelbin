/* Maintenance page (templates/maintenance.html).
   Design: DESIGN.md 16, mockups dev/mockups/29-ops-desktop.html +
   30-ops-mobile.html; rollout dev/changelog/444. Lifted out of settings.js when
   the four panels moved off Settings - the behavior is unchanged apart from the
   two blocking browser dialogs becoming buildModal(), and Storage and Index
   adopting the approved renderings.

   One updater per DOM region, per CLAUDE.md's frontend rules: #search-index-rows
   has exactly one writer and #storage-details-content has exactly one, so a poll
   tick and a manual refresh can never leave the panel describing two states. */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);

  const bootEl = $('#maintenance-boot');
  if (!bootEl) return;
  const BOOT = JSON.parse(bootEl.textContent);

  const toast = (msg, isError) =>
    showToast(msg, { type: isError ? 'error' : 'success', durationMs: 2500 });

  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

  // ── Backup & Restore ────────────────────────────────────────────────────
  const backupSel = $('#backup-select');

  const updateBackupButtons = () => {
    const has = backupSel.value !== '';
    $('#btn-show-diff').disabled = !has;
    $('#btn-apply-backup').disabled = !has;
  };

  const loadBackups = () =>
    jsonFetch('/api/settings/backups')
      .then((backups) => {
        backupSel.innerHTML = '<option value="">- select a backup -</option>';
        backups.forEach((b) => {
          const opt = document.createElement('option');
          opt.value = b.filename;
          opt.textContent = `${b.created_at}  (${b.filename})`;
          backupSel.appendChild(opt);
        });
        $('#backup-list-meta').textContent = backups.length
          ? `${plural(backups.length, 'backup')} available.`
          : 'No backups found yet.';
        updateBackupButtons();
      })
      .catch(() => { $('#backup-list-meta').textContent = 'Could not read the backup list.'; });

  backupSel.addEventListener('change', updateBackupButtons);

  (() => {
    const { hour, enabled } = BOOT.backup;
    const label = $('#backup-schedule-label');
    if (!enabled) { label.textContent = 'Automatic backups are disabled.'; return; }
    // A whole-hour local wall clock, not an instant, so it is rendered by hand rather than
    // through fmtTimeTz - only the 12h/24h choice is shared.
    const timeStr = displayHour12()
      ? `${hour % 12 || 12}:00 ${hour < 12 ? 'AM' : 'PM'}`
      : `${String(hour).padStart(2, '0')}:00`;
    label.textContent = `Scheduled: daily at ${timeStr}`;
  })();

  $('#btn-backup-now').addEventListener('click', (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = 'Backing up...';
    jsonFetch('/api/settings/backup', { method: 'POST' })
      .then((d) => { toast(`Backup created: ${d.filename}`); return loadBackups(); })
      .catch((err) => toast(`Backup failed: ${err.message || 'unknown'}`, true))
      .finally(() => { btn.disabled = false; btn.textContent = 'Back up now'; });
  });

  const diffLineHtml = (line) => {
    let cls = '';
    if (line.startsWith('+++') || line.startsWith('---')) cls = 'diff-header';
    else if (line.startsWith('@@')) cls = 'diff-hunk';
    else if (line.startsWith('+')) cls = 'diff-add';
    else if (line.startsWith('-')) cls = 'diff-remove';
    return `<div class="diff-line ${cls}">${escHtml(line)}</div>`;
  };

  const applyBackup = (filename, close) =>
    jsonFetch('/api/settings/rollback', {
      method: 'POST',
      body: JSON.stringify({ filename }),
    })
      .then(() => {
        if (close) close();
        toast('Config rolled back - reloading the page...');
        setTimeout(() => window.location.reload(), 1300);
      })
      .catch((e) => toast(`Rollback failed: ${e.message || 'unknown'}`, true));

  const openDiffModal = (filename) => {
    const modal = buildModal({
      title: `Diff - ${filename}`,
      body: '<p class="diff-empty">Loading...</p>',
      panelClass: 'modal-wide',
      footer: [
        { label: 'Cancel', class: 'btn' },
        {
          label: 'Apply backup',
          class: 'btn btn-danger',
          onClick: (close) => { applyBackup(filename, close); return false; },
        },
      ],
    });
    const body = $('.modal-body', modal);
    jsonFetch(`/api/settings/diff/${encodeURIComponent(filename)}`)
      .then((d) => {
        const lines = d.diff || [];
        body.innerHTML = lines.length
          ? `<p class="diff-legend"><span style="color:var(--bad)">- red</span> = removed from the
              current config when this backup is applied &nbsp;
              <span style="color:var(--ok)">+ green</span> = restored from the backup</p>
             <div class="diff-container">${lines.map(diffLineHtml).join('')}</div>`
          : '<p class="diff-empty">No differences - this backup is identical to the current config.</p>';
      })
      .catch((e) => {
        body.innerHTML = `<p class="diff-empty">Failed to load the diff: ${escHtml(e.message || 'unknown')}</p>`;
      });
  };

  $('#btn-show-diff').addEventListener('click', () => {
    if (backupSel.value) openDiffModal(backupSel.value);
  });
  // Applying goes through the diff too: the diff is what makes "apply" an informed
  // choice rather than a blind overwrite of the running config.
  $('#btn-apply-backup').addEventListener('click', () => {
    if (backupSel.value) openDiffModal(backupSel.value);
  });

  // ── Service control ─────────────────────────────────────────────────────
  // The server refuses (409) while a recording is capturing, joining or converting,
  // and returns the blocking rows so this can name them and offer to restart anyway.
  const restartBtn = $('#btn-restart-now');

  const resetRestartBtn = () => {
    restartBtn.disabled = false;
    restartBtn.textContent = 'Restart service now';
  };

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
    const statusEl = $('.restart-wait-status', body);
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
      $('.modal-panel', modal).appendChild(foot);
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

  const doRestart = (force) => {
    restartBtn.disabled = true;
    restartBtn.textContent = 'Restarting...';
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
                onClick: (close) => { proceeded = true; close(); doRestart(true); },
              },
            ],
            onClose: () => {
              if (proceeded) return;
              toast('Restart cancelled.');
              resetRestartBtn();
            },
          });
          return undefined;
        }
        // A dropped connection here is the restart working; a real error carries a status.
        if (e.status) {
          toast(`Restart failed: ${e.message || 'unknown'}`, true);
          resetRestartBtn();
        } else {
          showRestartWaitModal();
        }
        return undefined;
      });
  };

  const openRestartConfirm = (parkedHtml) => {
    buildModal({
      title: 'Restart the service?',
      body: `<p>Restart the ChannelBin service now? Active page connections will briefly drop.</p>
             ${BOOT.is_docker ? `<p class="restart-wait-warn">The container comes back only if
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
          onClick: (close) => { close(); doRestart(false); },
        },
      ],
    });
  };

  // Parked rows are asked for before the confirm opens because the POST never refuses over
  // them: this dialog is the only place a restart with just a parked conversion in flight
  // can say what it will cost. A failed lookup says so rather than implying nothing is parked.
  restartBtn.addEventListener('click', () => {
    jsonFetch('/api/settings/restart-parked')
      .then((data) => openRestartConfirm(parkedSection(data && data.parked)))
      .catch(() => openRestartConfirm(`<p class="restart-wait-warn">Could not check for
        parked conversions, so a restart may discard one that is part-way done.</p>`));
  });

  // ── Search index ────────────────────────────────────────────────────────
  // One updater for #search-index-rows. The poll only runs while a rebuild is in
  // flight - a programs rebuild measured 76s, so the panel would otherwise sit on a
  // stale readout for over a minute with no way to know it had finished.
  const indexRowsEl = $('#search-index-rows');
  const rebuildBtn = $('#btn-rebuild-index');
  let indexPollTimer = null;

  // Every status the server can send is named. A new one must land here rather than
  // in a trailing else, or it renders as a real state it is not. `ready` is asked
  // per index and is NOT the same as `status`: OK-but-stale reports as healthy on
  // `status` alone, and hiding that is the silence this panel exists to end.
  const indexPill = (ix) => {
    if (ix.status === 'BUILDING') return ['warn', 'REBUILDING'];
    if (ix.status === 'FAILED') return ['bad', 'FAILED'];
    if (ix.status === 'NEVER_BUILT') return ['warn', 'NEVER BUILT'];
    if (ix.status === 'OK') return ix.ready ? ['ok', 'READY'] : ['warn', 'STALE'];
    return ['warn', ix.status];
  };

  const DASH = '--';

  const renderIndexRows = (data) => {
    indexRowsEl.innerHTML = data.indexes.map((ix) => {
      const [cls, label] = indexPill(ix);
      // The reason string is the server's own wording (search_index_readiness),
      // deliberately not re-worded here - two spellings of one condition drift apart.
      const why = !ix.ready && ix.reason
        ? `<span class="ix-why">${escHtml(ix.reason)}</span>` : '';
      const err = ix.error
        ? `<span class="ix-why bad">Error: ${escHtml(ix.error)}</span>` : '';
      return `<div class="ix-row">
        <span class="ix-name">${escHtml(ix.label)}</span>
        <span class="h-mini ${cls}">${escHtml(label)}</span>
        <span class="ix-meta">${ix.row_count === null ? DASH : `${ix.row_count.toLocaleString()} rows`}</span>
        <span class="ix-meta">${ix.duration_ms === null ? DASH : fmtDur(ix.duration_ms / 1000)}</span>
        <span class="ix-meta">${ix.rebuilt_at === null ? 'never built' : escHtml(ix.rebuilt_at)}</span>
        ${why}${err}
      </div>`;
    }).join('');
    const busy = data.rebuilding || data.indexes.some((ix) => ix.status === 'BUILDING');
    rebuildBtn.disabled = busy;
    rebuildBtn.textContent = busy ? 'Rebuilding...' : 'Rebuild now';
    if (busy && indexPollTimer === null) {
      indexPollTimer = setInterval(loadIndexStatus, 3000);
    } else if (!busy && indexPollTimer !== null) {
      clearInterval(indexPollTimer);
      indexPollTimer = null;
    }
  };

  function loadIndexStatus() {
    return jsonFetch('/api/settings/search-index')
      .then(renderIndexRows)
      .catch((e) => {
        indexRowsEl.innerHTML =
          `<div class="mrow"><span class="grow">Could not read index status: ${escHtml(e.message || 'unknown')}</span></div>`;
      });
  }

  // A rebuild that starts AFTER this page loaded - which is how every account sync ends -
  // would otherwise leave READY rows and an enabled Rebuild button on screen for its whole
  // run, because the poll above only ever starts when a fetch has already found one in
  // flight. /api/nav-status carries index readiness to every page in the app, so a change
  // in what it reports is the signal that this card's own status is worth re-reading.
  //
  // This hook never writes #search-index-rows: it calls the one loader, which stays the
  // region's single writer and keeps owning both ends of the poll. The name is per page -
  // the channel search page defines its own - and it is registered inside this IIFE, which
  // only runs on the Maintenance page.
  //
  // The comparison is on the WHOLE answer, not on `ready` alone. A sync moves the source
  // watermark before it rebuilds, so an index goes stale (ready false) and only then
  // BUILDING (ready still false): watching the boolean would fire on the staleness and
  // then sleep through the rebuild this exists to catch. The reason string names which of
  // the four states it is in, so it moves when the boolean does not.
  let readinessSig = null;
  window.__applySearchReadiness = (payload) => {
    if (!payload) return;
    const sig = JSON.stringify(payload);
    if (sig === readinessSig) return;
    const first = readinessSig === null;
    readinessSig = sig;
    // The first payload is the baseline - the page's own boot load already covers it. And
    // while the 3s poll runs it is already re-reading this card, so the hook stands off.
    if (first || indexPollTimer !== null) return;
    loadIndexStatus();
  };

  rebuildBtn.addEventListener('click', () => {
    rebuildBtn.disabled = true;
    jsonFetch('/api/settings/search-index/rebuild', { method: 'POST' })
      .then(() => {
        toast('Rebuilding the search indexes - this can take a couple of minutes.');
        return loadIndexStatus();
      })
      .catch((e) => {
        // 409 carries the server's reason (a sync is running, or a rebuild already is).
        toast(e.message || 'Rebuild failed', true);
        return loadIndexStatus();
      });
  });

  // ── Storage ─────────────────────────────────────────────────────────────
  const storageEl = $('#storage-details-content');

  const srow = (k, v, cls) =>
    `<div class="srow"><span class="sk">${escHtml(k)}</span>
       <span class="sv${cls ? ` ${cls}` : ''}">${escHtml(v)}</span></div>`;

  // A directory the server could not measure reports null rather than 0. Keeping
  // the row and dashing the value says so: "we did not measure this" and "this is
  // empty" are different answers and the panel must not collapse them into one.
  const bytesOrDash = (n) => (n === null || n === undefined ? DASH : fmtBytes(n));

  // Drawn only when the server sent both ends of the proportion; an unmounted /dvr
  // sends null for each, and a meter at 0% would be a number nothing backs.
  const diskMeterHtml = (d) => {
    if (!d.disk_total || d.disk_free === null || d.disk_free === undefined) return '';
    const usedPct = Math.round((d.disk_total - d.disk_free) / d.disk_total * 100);
    const cls = usedPct >= 90 ? 'bad' : usedPct >= 75 ? 'warn' : '';
    // `stacked` is load-bearing, not decorative: without it the meter is a flex
    // item beside .disk-head, sized by content, and its percentage fill resolves
    // against zero width. See the rule's comment in maintenance.html.
    return `<div class="mrow stacked">
      <div class="disk-head">
        <span class="text-muted">Disk on ${escHtml(d.dvr_dir || '')}</span>
        <span class="mono"><strong>${usedPct}% used</strong> &middot;
          ${fmtBytes(d.disk_free)} free of ${fmtBytes(d.disk_total)}</span>
      </div>
      <div class="meter ${cls}"><span style="width:${usedPct}%"></span></div>
    </div>`;
  };

  const diskFreeClass = (d) => {
    if (!d.disk_total) return '';
    const usedPct = (d.disk_total - d.disk_free) / d.disk_total * 100;
    return usedPct >= 90 ? 'bad' : usedPct >= 75 ? 'warn' : 'ok';
  };

  const loadStorageDetails = () => {
    storageEl.innerHTML = '<div class="mrow"><span class="grow">Loading...</span></div>';
    return jsonFetch('/api/system/storage-details')
      .then((d) => {
        const stats = [
          srow('Recordings', bytesOrDash(d.dvr_bytes)),
          srow('Files', d.dvr_file_count === null || d.dvr_file_count === undefined
            ? DASH : d.dvr_file_count.toLocaleString()),
          srow('Database', bytesOrDash(d.db_bytes)),
          srow('Images', bytesOrDash(d.images_bytes)),
          srow('Config backups', bytesOrDash(d.backup_bytes)),
        ];
        if (d.disk_free !== null && d.disk_free !== undefined) {
          stats.push(srow('Disk free', fmtBytes(d.disk_free), diskFreeClass(d)));
        }
        // The paths are what turn a number into something actionable, and they are
        // long enough that they get their own full-width row rather than a value
        // column that word-breaks a /dvr path down to one character per line.
        const paths = [];
        if (d.dvr_dir) paths.push(`<div class="srow wide"><span class="sk">Recordings directory</span>
          <span class="sv">${escHtml(d.dvr_dir)}</span></div>`);
        if (d.images_dir) paths.push(`<div class="srow wide"><span class="sk">Images directory</span>
          <span class="sv">${escHtml(d.images_dir)}</span></div>`);
        if (d.db_path) paths.push(`<div class="srow wide"><span class="sk">Database path</span>
          <span class="sv">${escHtml(d.db_path)}</span></div>`);
        storageEl.innerHTML = diskMeterHtml(d) +
          `<div class="statlist${paths.length ? ' has-wide' : ''}">${stats.join('')}${paths.join('')}</div>`;
      })
      .catch(() => {
        storageEl.innerHTML = '<div class="mrow"><span class="grow">Failed to load storage details.</span></div>';
      });
  };

  $('#btn-refresh-storage').addEventListener('click', loadStorageDetails);

  // ── External tools ──────────────────────────────────────────────────────
  // Reuses .statlist/.srow above rather than styling of its own, so the card reads
  // as the same kind of readout Storage is and restacks at 375px for free.
  const toolsEl = $('#tools-content');

  // Where the binary came from, which is the half of the answer a path does not give.
  // A fourth value, `bundled`, named the imageio-ffmpeg fallback until dev/changelog/911
  // removed it - the server no longer sends it, and an unknown key already falls through
  // to no provenance line rather than rendering `undefined`. `configured` names the setting
  // from the payload rather than a literal, because the two binaries have different keys
  // and a card that told an operator to edit ffmpeg.path to move ffprobe would be sending
  // them to the wrong field (dev/changelog/914).
  const toolSource = (t) => {
    if (t.source === 'path') return 'found on PATH';
    if (t.source === 'sibling') return 'found beside the configured ffmpeg';
    if (t.source === 'configured') return `set in ${t.config_key}`;
    return '';
  };

  // Every row is full-width, so the four read down the card as two labelled pairs
  // (ffmpeg, its path, ffprobe, its path). The default two-column .statlist put the
  // pair of version rows side by side above two full-width path rows, which broke the
  // association between a binary and the path it was found at.
  const toolVersionRow = (t) => (t.found
    ? `<div class="srow wide"><span class="sk">${escHtml(t.name)}</span>
      <span class="sv ok">${escHtml(t.version || 'unknown version')}</span></div>`
    : `<div class="srow wide"><span class="sk">${escHtml(t.name)}</span>
      <span class="sv bad">not found</span></div>`);

  const toolPathRow = (t) => {
    const suffix = t.found && toolSource(t) ? ` (${toolSource(t)})` : '';
    return `<div class="srow wide"><span class="sk">${escHtml(t.name)} path</span>
      <span class="sv">${escHtml(t.path)}${escHtml(suffix)}</span></div>`;
  };

  // The alert raised at startup says the same thing, but an operator who is already
  // looking at this card should not have to go find it - and a card that shows "not
  // found" without saying what breaks or what to do is half an answer.
  const toolsWarning = (missing) => {
    if (!missing.length) return '';
    const what = missing.includes('ffmpeg')
      ? 'Recordings and health checks cannot run at all.'
      : 'Recording still works, but nothing can be probed: no format detection, no '
        + 'recording health numbers, and no channel-group format matching.';
    return `<div class="mrow"><span class="grow"><strong>${escHtml(missing.join(' and '))}</strong>
      could not be run. ${escHtml(what)} Install ffmpeg on this machine (it supplies both
      binaries), or set <code>ffmpeg.path</code> in Settings to an absolute path - ffprobe is
      taken from the same directory automatically.</span></div>`;
  };

  // What the resolved ffmpeg build includes, limited to the components ChannelBin invokes.
  // Three states, never two: `available: null` means the listing that would answer it could
  // not be read, and rendering that as "not in this build" would be a false alarm. The line
  // under the label says what uses the component, or what breaks when it is absent.
  const capabilityStatus = (c) => {
    if (c.available === true) return ['ok', 'in this build'];
    if (c.available === false) return ['bad', 'not in this build'];
    return ['warn', 'could not be checked'];
  };

  const capabilityRows = (caps) => {
    if (!caps || !caps.length) return '';
    const rows = caps.map((c) => {
      const [cls, text] = capabilityStatus(c);
      const sub = c.available === false ? c.without : c.used_for;
      return `<div class="srow wide"><span class="sk">${escHtml(c.label)}<span class="srow-sub">${escHtml(sub)}</span></span>
        <span class="sv ${cls}">${escHtml(text)}</span></div>`;
    }).join('');
    return `<div class="mrow"><span class="grow">What this ffmpeg build includes, limited to
      the parts ChannelBin uses.</span></div><div class="statlist has-wide">${rows}</div>`;
  };

  const loadTools = () => {
    toolsEl.innerHTML = '<div class="mrow"><span class="grow">Loading...</span></div>';
    return jsonFetch('/api/system/tools')
      .then((d) => {
        const tools = [d.tools.ffmpeg, d.tools.ffprobe];
        toolsEl.innerHTML = toolsWarning(d.missing || [])
          + `<div class="statlist has-wide">${tools.map(
            (t) => toolVersionRow(t) + toolPathRow(t)).join('')}</div>`
          + capabilityRows(d.capabilities);
      })
      .catch(() => {
        toolsEl.innerHTML = '<div class="mrow"><span class="grow">Failed to load external tool versions.</span></div>';
      });
  };

  // ── Support bundle ──────────────────────────────────────────────────────
  // The opt-in rides on the download link's href rather than a form post, so the
  // server decides from ?names=1 alone and the link keeps working with JS off (as
  // the safe, pseudonymized default). Server-side enforcement, per CLAUDE.md: the
  // route reads the parameter itself and never trusts the page's state.
  const bundleNames = $('#bundle-names');
  const bundleLink = $('#bundle-download');
  if (bundleNames && bundleLink) {
    const bundleHref = bundleLink.getAttribute('href');
    bundleNames.addEventListener('change', () => {
      bundleLink.setAttribute('href', bundleNames.checked ? `${bundleHref}?names=1` : bundleHref);
    });
  }

  // ── Boot ────────────────────────────────────────────────────────────────
  loadBackups();
  loadIndexStatus();
  loadStorageDetails();
  loadTools();
})();
