/* The Readiness check card on Maintenance: what this install can and cannot do, and what
   stands behind each answer. Server side is app/readiness.py, which owns the registries,
   the states and the verdict sentence. Rules and design record: dev/changelog/950, built
   from dev/mockups/40-readiness.html.

   ONE updater for the card: render(). Every action mutates `state` and calls it; nothing
   writes into #rd-content anywhere else. Events are delegated on the card, because render()
   replaces its innerHTML and per-element handlers bound once at load would be destroyed by
   the first rebuild.

   The nav counts are NOT written here. base.html owns those nodes and exposes
   window.__applyReadiness as their one writer - this card hands it the `nav` block of
   whatever payload it just fetched, so silencing a check moves the badge on the click
   rather than at the next poll. */
(() => {
  'use strict';

  const card = document.getElementById('m-readiness');
  if (!card) return;

  const content = document.getElementById('rd-content');
  const headPill = document.getElementById('rd-head-pill');
  const runAllBtn = document.getElementById('rd-runall');
  const copyBtn = document.getElementById('rd-copy');

  /* The seven states, every one named. `checking` is the client-side transient while a run
     is in flight - the server never sends it - and it is listed here rather than left to a
     trailing else, so the next state added has to be given a label instead of silently
     rendering as something it is not. */
  const STATUS = {
    ready:     { label: 'Ready', cls: 'b-done',
                 tip: 'Checked, and it is fine.' },
    unknown:   { label: 'Could not check', cls: 'b-warn',
                 tip: 'The check itself could not run.\nThat is not the same as passing, so it is never shown as one.' },
    attention: { label: 'Attention', cls: 'b-warn',
                 tip: 'It works today, but it is degraded or it will bite you later.' },
    problem:   { label: 'Problem', cls: 'b-fail',
                 tip: 'Something is broken right now.' },
    nothing:   { label: 'Nothing to check', cls: 'b-abort',
                 tip: 'There is nothing on this install for this check to look at.' },
    not_run:   { label: 'Not run yet', cls: 'b-abort',
                 tip: 'This one costs real work - a process, a provider login, a message - so it\nonly runs when you ask. Nothing expensive happens because you opened the page.' },
    checking:  { label: 'Checking', cls: 'b-running', tip: 'Running right now.' },
  };

  const CAP_CLASS = {
    can: 'rd-cap-can', cannot: 'rd-cap-cannot', degraded: 'rd-cap-degraded',
    unknown: 'rd-cap-unknown', none: 'rd-cap-none',
  };

  const state = {
    data: null,
    open: new Set(),      // capability ids the user has expanded
    rows: new Set(),      // `${capId}:${checkId}` rows the user has expanded
    checking: new Set(),  // checks with a run in flight
    busy: new Set(),      // checks with a one-click fix in flight
    progress: null,
  };

  const byId = (id) => (state.data ? state.data.checks.find((c) => c.id === id) : null);
  const plural = (n, one, many) => (n === 1 ? one : (many || `${one}s`));

  function pill(status) {
    const s = STATUS[status] || STATUS.unknown;
    return `<span class="badge ${s.cls}" data-tip="${escHtml(s.tip)}">${
      status === 'checking' ? '<span class="pulse"></span>' : ''}${escHtml(s.label)}</span>`;
  }

  function statusOf(row) {
    return state.checking.has(row.id) ? 'checking' : row.status;
  }

  /* The reason line. When several things block one capability, every one of them is named
     on its own line rather than the first being named and the rest counted
     (dev/changelog/950). An ignored check never reaches here - the server left it out of
     the capability's state entirely. */
  function reasonLine(cap) {
    const rows = (ids) => ids.map(byId).filter(Boolean);
    const noun = (row) => {
      const short = row.short || row.label;
      // ffmpeg and ffprobe are command names and are never capitalized, not even at the
      // start of a sentence.
      if (short.startsWith('ffmpeg') || short.startsWith('ffprobe')) return short;
      return short.charAt(0).toUpperCase() + short.slice(1);
    };
    const list = (items) => `<ul class="rd-whylist">${items.map((r) =>
      `<li><strong>${escHtml(noun(r))}</strong> &middot; ${escHtml(r.found)}</li>`).join('')}</ul>`;
    const one = (r) => `${escHtml(noun(r))}: ${escHtml(r.found)}`;

    const blockers = rows(cap.blockers);
    if (blockers.length) {
      const lead = '<span class="rd-lead-bad">No.</span>';
      if (blockers.length > 1) {
        return `${lead} ${blockers.length} things are stopping this:${list(blockers)}`;
      }
      return `${lead} ${one(blockers[0])}`;
    }
    const soft = rows(cap.degraded);
    if (soft.length) {
      const lead = '<span class="rd-lead-warn">Yes, but not fully.</span>';
      if (soft.length > 1) {
        return `${lead} ${soft.length} things are degraded:${list(soft)}`;
      }
      return `${lead} ${one(soft[0])}`;
    }
    const unsure = rows(cap.unknown);
    if (unsure.length) {
      return `Not known. ${escHtml(noun(unsure[0]))} could not be checked at all, and a check
        that failed to answer is never counted as a pass.`;
    }
    if (cap.state === 'none') {
      const nothing = rows(cap.nothing);
      return `Nothing is set up for this yet. ${escHtml(noun(nothing[0]))}: ${escHtml(nothing[0].found)}`;
    }
    const running = cap.needs.filter((id) => state.checking.has(id));
    if (running.length) {
      const row = byId(running[0]);
      return `Yes so far. Checking ${escHtml(row.short || row.label)} right now.`;
    }
    const notRun = rows(cap.not_run);
    if (notRun.length) {
      return `Yes, as far as everything that has been checked. ${notRun.length}
        ${plural(notRun.length, 'check behind this costs', 'checks behind this cost')} real work
        and ${plural(notRun.length, 'has', 'have')} not been run.`;
    }
    const ignored = cap.ignored.length
      ? ` <span class="rd-dv">(${cap.ignored.length} ignored)</span>` : '';
    return `Yes. ${cap.needs.length} ${plural(cap.needs.length, 'check')} behind this, all ready.${ignored}`;
  }

  /* One click here, or a link to the page that owns it - never both on one row. The server
     decides which by sending exactly one of `action` and `link`. */
  function fixButtons(row) {
    const status = statusOf(row);
    if (status === 'ready' || status === 'nothing' || status === 'checking') return '';
    if (status === 'not_run') {
      return `<button class="btn btn-sm btn-primary" data-run="${escHtml(row.id)}">Run it</button>`;
    }
    if (state.busy.has(row.id)) {
      return '<span class="rd-dv">Working...</span>';
    }
    if (row.action) {
      return `<button class="btn btn-sm btn-primary" data-fix="${escHtml(row.id)}">${
        escHtml(row.action.label)}</button>`;
    }
    if (row.link) {
      return `<a class="btn btn-sm" href="${escHtml(row.link.url)}">${
        escHtml(row.link.label)} <span aria-hidden="true">&rarr;</span></a>`;
    }
    return '';
  }

  function ignoreButton(row) {
    if (!row.ignorable) return '';
    if (row.ignored) {
      return `<button class="btn btn-sm" data-unignore="${escHtml(row.id)}"
        data-tip="Count this one again.">Stop ignoring</button>`;
    }
    if (statusOf(row) === 'ready') return '';
    return `<button class="btn btn-sm" data-ignore="${escHtml(row.id)}"
      data-tip="Stop counting this one. It stays on the page, dimmed, with whatever it
found still shown - it just stops blocking the verdict and stops adding to the
count beside Maintenance.">Ignore this</button>`;
  }

  function detailBlock(row) {
    const status = statusOf(row);
    const lines = [['What was checked', escHtml(row.tested), '']];
    lines.push(['What came back', escHtml(row.found),
      status === 'problem' ? 'bad' : (status === 'attention' ? 'warn' : '')]);
    if (status !== 'ready' && status !== 'nothing') {
      lines.push(['What that costs you', escHtml(row.without), '']);
    }
    if (row.note && (status === 'not_run' || status === 'checking')) {
      lines.push(['Why it waits', escHtml(row.note), '']);
    }
    if (row.ignored) {
      lines.push(['Ignored', 'You told ChannelBin not to count this one. It is still checked '
        + 'and still shown; it just does not block the verdict or add to the count beside '
        + 'Maintenance. It stays ignored until you say otherwise.', '']);
    }
    if (row.link && row.action) {
      lines.push(['Where it lives', escHtml(row.link.label), '']);
    }
    if (Array.isArray(row.detail) && row.detail.length) {
      lines.push(['Component by component', row.detail.map((c) => {
        const good = c.available === true;
        const mark = good ? '✓' : (c.available === null ? '?' : '×');
        return `<div class="rd-comp"><span class="${good ? 'rd-comp-ok' : 'rd-comp-x'}">${mark}</span>
          <span><strong>${escHtml(c.label)}</strong> &middot; ${escHtml(c.used_for)}${
          good ? '' : `<br><span class="rd-dv bad">${escHtml(c.without)}</span>`}</span></div>`;
      }).join(''), '']);
    }
    lines.push(['Last checked', row.last_run
      ? escHtml(fmtTimeTz(new Date(row.last_run)))
      : (status === 'not_run' ? 'never - it waits for you' : 'when this card loaded'), '']);
    return `<div class="rd-detail">${lines.map(([k, v, cls]) =>
      `<div class="rd-dk">${escHtml(k)}</div><div class="rd-dv ${cls}">${v}</div>`).join('')}</div>`;
  }

  function checkRow(capId, row) {
    const key = `${capId}:${row.id}`;
    const open = state.rows.has(key);
    const status = statusOf(row);
    return `<div class="rd-row s-${escHtml(status)}${open ? ' open' : ''}${
      row.ignored ? ' rd-muted' : ''}" data-row="${escHtml(key)}">
      <div class="rd-rname" data-rowtoggle="${escHtml(key)}"><span class="rd-rcaret">&#9656;</span>${
      escHtml(row.label)}</div>
      <div class="rd-rfound">${pill(status)} ${escHtml(row.found)}</div>
      <div class="rd-ract">${fixButtons(row)}${ignoreButton(row)}</div>
      ${open ? detailBlock(row) : ''}
    </div>`;
  }

  /* One capability. `.grp-item` and its children are style.css's own expanding row - the
     same anatomy the Channel Groups page uses - so the left-edge state colour comes from
     `data-health` with no rule of this card's own. Rows are NOT auto-opened: eleven rows with
     four open is a long card, and the reason line already says how many are wrong
     (dev/changelog/950). */
  function capRow(cap) {
    const open = state.open.has(cap.id);
    const bad = cap.blockers.length + cap.degraded.length + cap.unknown.length;
    const rows = cap.needs.map(byId).filter(Boolean);
    const single = bad === 1;
    const worst = byId(cap.blockers[0] || cap.degraded[0] || cap.unknown[0] || cap.not_run[0]);
    // One blocker gets its fix on the summary row. Several do not: there is no one button
    // for three different problems, so the row gets a count and the fixes live on the
    // individual checks inside it.
    const act = worst && single ? fixButtons(worst) : '';
    const chip = bad > 1
      ? `<span class="chip" data-tip="Open this row to see each one, what it costs you and how to fix it.">${bad} to fix</span>`
      : '';
    const order = ['problem', 'unknown', 'attention', 'not_run', 'nothing', 'ready'];
    const ordered = rows.slice().sort((a, b) =>
      (order.indexOf(statusOf(a)) - order.indexOf(statusOf(b)))
      || (cap.needs.indexOf(a.id) - cap.needs.indexOf(b.id)));
    return `<div class="grp-item ${CAP_CLASS[cap.state]}" data-health="${escHtml(cap.health)}">
      <div class="grp-summary">
        <button type="button" class="grp-expand" aria-expanded="${open}"
                aria-label="Show the checks behind this" data-cap="${escHtml(cap.id)}"><span class="grp-caret"></span></button>
        <div class="rd-caprow" data-cap="${escHtml(cap.id)}">
          <div class="rd-capmark" aria-hidden="true">${escHtml(cap.mark)}</div>
          <div class="rd-capname">${escHtml(cap.label)}</div>
          <div class="rd-capwhy">${reasonLine(cap)}</div>
          <div class="rd-capact">${chip}${act}</div>
        </div>
      </div>
      ${open ? `<div class="grp-detail">
        <div class="rd-rows">${ordered.map((r) => checkRow(cap.id, r)).join('')}</div>
      </div>` : ''}
    </div>`;
  }

  function headPillFor(d) {
    const blocked = d.nav.blocked;
    const degraded = d.nav.degraded;
    const unsure = d.capabilities.filter((c) => c.state === 'unknown').length;
    if (blocked) {
      return ['b-fail', 'Not ready',
        `${blocked} ${plural(blocked, 'thing')} you would want to do cannot be done right now.`];
    }
    if (degraded) {
      return ['b-warn', 'Degraded',
        'Everything works. Some of it is degraded or heading somewhere bad.'];
    }
    if (unsure) {
      return ['b-warn', 'Unknown',
        'A check could not answer, so nothing here claims to be ready.'];
    }
    return ['b-done', 'Ready', 'Every check that counts passed.'];
  }

  function render() {
    const d = state.data;
    if (!d) return;
    const [cls, label, tip] = headPillFor(d);
    headPill.className = `badge ${cls}`;
    headPill.textContent = label;
    headPill.setAttribute('data-tip', tip);

    const notRun = d.counts.not_run;
    runAllBtn.textContent = notRun
      ? `Run ${notRun} ${plural(notRun, 'check')} that ${plural(notRun, 'costs', 'cost')} something`
      : 'Run every check again';

    const v = d.verdict;
    const c = d.counts;
    const meta = [`${c.counted} checks behind ${d.capabilities.length} things you might want to do`,
      `${c.ready} ready`];
    if (c.attention) meta.push(`${c.attention} need attention`);
    if (c.problem) meta.push(`${c.problem} ${plural(c.problem, 'problem')}`);
    if (c.unknown) meta.push(`${c.unknown} could not be checked`);
    if (c.not_run) meta.push(`${c.not_run} not run yet`);
    // Always named, even at zero-free: the point of silencing is that the count stops
    // shouting, not that the page stops admitting it was told to look away.
    if (c.ignored) meta.push(`${c.ignored} ignored`);

    const progress = state.progress
      ? `<div class="rd-progress"><div class="rd-bar"><span style="width:${state.progress.pct}%"></span></div>
         <span>${escHtml(state.progress.label)}</span></div>`
      : '';

    content.innerHTML = `<div class="rd-verdict ${escHtml(v.level)}">
        <div class="rd-vmark" aria-hidden="true">${escHtml(v.mark)}</div>
        <div class="rd-vbody">
          <div class="rd-vhead">${escHtml(v.head)}</div>
          <div class="rd-vsub">${escHtml(v.sub)}</div>
          <div class="rd-vmeta">${escHtml(meta.join(' · '))}</div>
        </div>
      </div>
      ${progress}
      <p class="rd-intro">What this install can and cannot do, and what stands behind each
        answer. Open a row to see every check behind it, what it tested and how to fix it.</p>
      <div class="rd-caps">${d.capabilities.map(capRow).join('')}</div>`;
  }

  function apply(payload) {
    state.data = payload;
    // base.html owns the nav badge and is its one writer; this hands it the numbers that
    // arrived with the payload so a silenced check stops counting on the click.
    if (window.__applyReadiness) window.__applyReadiness(payload.nav);
    render();
  }

  function load() {
    jsonFetch('/api/readiness')
      .then(apply)
      .catch((e) => {
        content.innerHTML = `<div class="mrow"><span class="grow">Could not load the readiness
          check: ${escHtml(e.message)}</span></div>`;
      });
  }

  function runCheck(id) {
    state.checking.add(id);
    render();
    return jsonFetch('/api/readiness/run', {
      method: 'POST', body: JSON.stringify({ check: id }),
    }).then((payload) => {
      state.checking.delete(id);
      apply(payload);
    }).catch((e) => {
      state.checking.delete(id);
      render();
      showToast(`That check could not be run: ${e.message}`, { type: 'error' });
    });
  }

  function runAll() {
    const pending = state.data.checks.filter((c) => c.status === 'not_run').map((c) => c.id);
    if (!pending.length) {
      // Nothing is waiting, so this is a re-run of the expensive ones rather than a first
      // run. The cheap checks never stopped being current - they are measured on every load.
      const all = state.data.checks.filter((c) => c.cost === 'ondemand').map((c) => c.id);
      return all.reduce((chain, id) => chain.then(() => runCheck(id)), Promise.resolve())
        .then(() => { state.progress = null; render(); showToast('Every check re-run.'); });
    }
    let done = 0;
    return pending.reduce((chain, id) => chain.then(() => {
      const row = byId(id);
      state.progress = { pct: Math.round((done / pending.length) * 100), label: row.label };
      return runCheck(id).then(() => { done += 1; });
    }), Promise.resolve()).then(() => {
      state.progress = null;
      render();
      showToast(`Ran ${pending.length} ${plural(pending.length, 'check')}.`);
    });
  }

  /* A one-click fix is a POST to the endpoint that already owns it - the account sync,
     the index rebuild, the notification test. Several URLs is still one click: "sync the
     2 stale accounts" is one button. */
  function applyFix(id) {
    const row = byId(id);
    if (!row || !row.action) return;
    state.busy.add(id);
    render();
    row.action.urls.reduce(
      (chain, url) => chain.then(() => jsonFetch(url, { method: 'POST' })),
      Promise.resolve(),
    ).then(() => {
      showToast(`${row.action.label} - done. Re-checking.`);
    }).catch((e) => {
      showToast(`That did not work: ${e.message}`, { type: 'error' });
    }).finally(() => {
      state.busy.delete(id);
      load();
    });
  }

  function setIgnored(id, ignored) {
    jsonFetch('/api/readiness/ignore', {
      method: 'POST', body: JSON.stringify({ check: id, ignored }),
    }).then(apply)
      .catch((e) => showToast(`Could not change that: ${e.message}`, { type: 'error' }));
  }

  function reportText() {
    const d = state.data;
    const verdict = { can: 'YES', cannot: 'NO', degraded: 'PARTLY', unknown: 'UNKNOWN',
                      none: 'NOT SET UP' };
    const lines = ['ChannelBin readiness check', `Generated ${d.generated_at}`, '',
      d.verdict.head, d.verdict.sub, '', '## What this install can and cannot do'];
    d.capabilities.forEach((cap) => {
      lines.push(`  [${verdict[cap.state]}] ${cap.label}`);
    });
    lines.push('');
    d.areas.forEach((area) => {
      lines.push(`## ${area.label}`);
      d.checks.filter((r) => r.area === area.id).forEach((r) => {
        const tag = STATUS[r.status].label + (r.ignored ? ', ignored' : '');
        lines.push(`  [${tag}] ${r.label}`);
        lines.push(`      checked: ${r.tested}`);
        lines.push(`      found:   ${r.found}`);
        if (r.status !== 'ready' && r.status !== 'nothing') {
          lines.push(`      cost:    ${r.without}`);
        }
      });
      lines.push('');
    });
    return lines.join('\n');
  }

  card.addEventListener('click', (ev) => {
    const run = ev.target.closest('[data-run]');
    if (run) { ev.stopPropagation(); runCheck(run.dataset.run); return; }
    const fix = ev.target.closest('[data-fix]');
    if (fix) { ev.stopPropagation(); applyFix(fix.dataset.fix); return; }
    const ignore = ev.target.closest('[data-ignore]');
    if (ignore) { ev.stopPropagation(); setIgnored(ignore.dataset.ignore, true); return; }
    const unignore = ev.target.closest('[data-unignore]');
    if (unignore) { ev.stopPropagation(); setIgnored(unignore.dataset.unignore, false); return; }
    if (ev.target.closest('#rd-runall')) { runAll(); return; }
    if (ev.target.closest('#rd-copy')) {
      if (navigator.clipboard) navigator.clipboard.writeText(reportText());
      showToast('Report copied.');
      return;
    }
    // A link out of the card is a real navigation, not a row toggle.
    if (ev.target.closest('a[href]')) return;
    // The check row inside an expanded capability is matched BEFORE the capability itself:
    // the inner row is nested in the outer one and both would otherwise match.
    const rowToggle = ev.target.closest('[data-rowtoggle]');
    if (rowToggle) {
      const key = rowToggle.dataset.rowtoggle;
      if (state.rows.has(key)) state.rows.delete(key); else state.rows.add(key);
      render();
      return;
    }
    const cap = ev.target.closest('[data-cap]');
    if (cap) {
      const id = cap.dataset.cap;
      if (state.open.has(id)) state.open.delete(id); else state.open.add(id);
      render();
    }
  });

  load();
})();
