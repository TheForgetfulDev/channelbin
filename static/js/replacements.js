/* The Replacements page (templates/channels/replacements.html, DESIGN-channel-successors.md
   section 9, mockup 66 approved in dev/changelog/1789). Draws one row per channel the last
   sync did not list from the server's boot block, with its proposals and what an accept would
   bring over, and sends accepts to /api/replacements/accept. The server re-checks every pair
   from database ids; this page only chooses which pairs to ask for.

   util.js supplies escHtml, nf, counted, joinLabels, icon, accountPill, healthBandCss,
   fmtDateTz, tzDayLabel, jsonFetch, buildModal, showToast, hideToast, readBootJson;
   filter-bar.js supplies createFilterBar. */
(() => {
  'use strict';

  const boot = readBootJson('rs-boot', {}) || {};
  const P = boot.payload || {};
  if (!P.rows || !P.rows.length) return;

  const byId = (id) => document.getElementById(id);
  const ch = (id) => P.channels[String(id)];
  const acct = (id) => P.accounts[String(id)] || { name: 'a deleted account', color: '' };
  const groupName = (gid) => P.groups[String(gid)] || 'Group';
  const at = (iso) => (iso ? new Date(iso.endsWith('Z') ? iso : `${iso}Z`) : null);
  const day = (iso) => (iso ? fmtDateTz(at(iso), { month: 'short', day: 'numeric' }) : '-');
  const when = (iso) => (iso ? `${tzDayLabel(at(iso))} ${fmtDateTz(at(iso), { hour: 'numeric', minute: '2-digit' })}` : 'never');
  const chLink = (c) => `<a href="/channels/${c.id}">${escHtml(c.name)}</a>`;
  const pill = (id) => accountPill({ name: acct(id).name, color: acct(id).color });

  let rows = P.rows.slice();     // the channels still waiting; an accepted one leaves
  const chosen = new Map();      // old id -> the proposal picked among several
  const handPicked = new Map();  // old id -> {id, name, account_id, account} from "Pick a channel"
  const picks = new Set();       // rows ticked for Accept selected
  const skipped = new Map();     // old id -> why the server refused its last accept
  let target = 'own';
  let filterBar = null;

  const carryOn = () => [...document.querySelectorAll('[data-carry]')]
    .filter((cb) => cb.checked).map((cb) => cb.dataset.carry);

  // Section 4: "Replace with channels from" filters the proposals and is never stored.
  function matchesFor(row) {
    const old = ch(row.old_id);
    const want = target === 'own' ? old.account_id : Number(target);
    return row.matches.filter((m) => ch(m.id).account_id === want);
  }
  function rowState(row) {
    if (handPicked.has(row.old_id)) return 'one';
    const m = matchesFor(row);
    if (!m.length) return 'none';
    if (m.length === 1) return 'one';
    return m.some((x) => x.id === chosen.get(row.old_id)) ? 'one' : 'several';
  }
  function pickOf(row) {
    const hp = handPicked.get(row.old_id);
    if (hp) return { id: hp.id, picked: true, carry: null };
    const m = matchesFor(row);
    if (m.length === 1) return m[0];
    return m.find((x) => x.id === chosen.get(row.old_id)) || null;
  }

  function scorePill(c) {
    if (c.score === null || c.score === undefined) {
      return '<span class="text-muted tip-plain" data-tip="No score.&#10;No test or recording has observed this channel.">-</span>';
    }
    const s = Math.round(c.score);
    return `<span class="num hb-text ${healthBandCss(s)} tip-plain" data-tip="Health score ${s}.&#10;Built from ${escHtml(counted(c.samples, 'observation'))}.">${icon('health-score')}${s}</span>`;
  }

  function matchCell(row) {
    const old = ch(row.old_id);
    const hp = handPicked.get(row.old_id);
    if (hp) {
      return `<a href="/channels/${hp.id}">${escHtml(hp.name)}</a>
        <div class="text-faint small">${escHtml(hp.account)} &middot; picked by hand</div>
        <button class="btn btn-sm rs-rechoose" type="button" data-act="pick" data-cid="${old.id}">Pick another</button>`;
    }
    const m = matchesFor(row);
    if (!m.length) {
      const where = target === 'own' ? acct(old.account_id).name : acct(Number(target)).name;
      return `<span class="text-muted">No replacement found on ${escHtml(where)}</span>
        <div class="text-faint small">No listed channel there carries stream id <code>${escHtml(old.psid || '-')}</code>.</div>
        <button class="btn btn-sm rs-pick" type="button" data-act="pick" data-cid="${old.id}">Pick a channel</button>`;
    }
    const pick = pickOf(row);
    if (pick) {
      const n = ch(pick.id);
      // One sentence per match kind (channel_successors.match_kind); a kind this page has
      // no words for says so rather than borrowing another's.
      const KIND_WORDS = {
        stream_id: `same stream id <code>${escHtml(n.psid || '-')}</code>`,
        url: 'same stream URL',
      };
      const how = KIND_WORDS[pick.kind] || `matched as ${escHtml(pick.kind)}`;
      return `${chLink(n)} ${scorePill(n)}
        <div class="text-faint small">${escHtml(acct(n.account_id).name)} &middot; ${how}${n.name === old.name ? '' : ' &middot; <span class="val-warn">another name</span>'}</div>
        ${m.length > 1 ? `<button class="btn btn-sm rs-rechoose" type="button" data-act="rechoose" data-cid="${old.id}">Choose another (${m.length})</button>` : ''}`;
    }
    return `<div class="rs-choose" role="radiogroup" aria-label="Replacements for ${escHtml(old.name)}">
      <div class="small rs-choose-h">${m.length} channels carry this stream. Pick one:</div>
      ${m.map((x) => { const n = ch(x.id); return `<label class="rs-opt"><input type="radio" name="rs-${old.id}" data-act="choose" data-cid="${old.id}" value="${n.id}"> ${escHtml(n.name)} ${scorePill(n)}
        <span class="text-faint small">${escHtml(acct(n.account_id).name)}</span></label>`; }).join('')}
    </div>`;
  }

  function carriesCell(row) {
    const why = skipped.get(row.old_id);
    const said = why ? `<div class="small val-warn">Not replaced: ${escHtml(why)}</div>` : '';
    return said + carriesWords(row);
  }

  function carriesWords(row) {
    const pick = pickOf(row);
    if (!pick) return '<span class="text-faint small">After you choose</span>';
    const n = row.groups.length;
    let h = `<div class="small">${n ? `${escHtml(counted(n, 'group'))}, same place and switches` : 'Its own guide row'}</div>`;
    if (!pick.carry) {
      return `${h}<div class="text-faint small">What the accept finds to bring over</div>`;
    }
    const on = new Set(carryOn());
    const carry = pick.carry.filter((p) => on.has(p.k) && p.state === 'carry').map((p) => p.label);
    const kept = pick.carry.filter((p) => on.has(p.k) && p.state === 'kept').map((p) => p.label);
    if (carry.length) h += `<div class="text-faint small">${escHtml(joinLabels(carry))}</div>`;
    if (kept.length) {
      h += `<div class="small val-warn tip-plain" data-tip="Kept its own.&#10;The new channel already has its own value for this, so the old one is not copied over it.">Kept its own: ${escHtml(joinLabels(kept))}</div>`;
    }
    return h;
  }

  function sortRows() {
    rows.sort((a, b) => {
      const x = ch(a.old_id);
      const y = ch(b.old_id);
      return acct(x.account_id).name.localeCompare(acct(y.account_id).name)
        || x.account_id - y.account_id || x.name.localeCompare(y.name) || x.id - y.id;
    });
  }

  function draw() {
    const tb = byId('rs-tbody');
    if (!rows.length) {
      byId('rs-main').innerHTML = '<div class="empty-state"><p>Every channel in your groups was in its account\'s last sync.</p><p class="text-muted">Nothing to replace.</p></div>';
      return;
    }
    let last = null;
    tb.innerHTML = rows.map((row) => {
      const old = ch(row.old_id);
      const a = acct(old.account_id);
      const st = rowState(row);
      let h = '';
      if (old.account_id !== last) {
        last = old.account_id;
        h += `<tr class="rs-acct" data-acct="${old.account_id}"><td colspan="6">${pill(old.account_id)}
          <span class="text-faint small">last synced ${escHtml(when(a.last_sync_at))}</span>
          ${a.shrunk ? '<span class="badge b-warn tip-plain" data-tip="Its last sync was much shorter than usual.&#10;A playlist that came back short drops channels it still has. Check the account before replacing anything on it.">Short playlist</span>' : ''}</td></tr>`;
      }
      const sel = picks.has(old.id);
      h += `<tr data-cid="${old.id}" data-state="${st}"${sel ? ' class="rs-picked"' : ''}>
        <td data-label="Select">${st === 'one' ? `<input type="checkbox" data-act="pick-row" data-cid="${old.id}"${sel ? ' checked' : ''} aria-label="Select ${escHtml(old.name)}">` : ''}</td>
        <td data-label="Not in the last sync">${chLink(old)} ${scorePill(old)}
          <div class="text-faint small">Last listed ${escHtml(day(old.last_seen))} &middot; stream id <code>${escHtml(old.psid || '-')}</code></div></td>
        <td data-label="In groups">${row.groups.map((g) => `<a class="mem-chip grp" href="/channel-groups/${g}"><span class="mc-glyph">${icon('group')}</span> ${escHtml(groupName(g))}</a>`).join(' ') || (old.in_guide ? '<span class="small">Own guide row</span>' : '')}</td>
        <td data-label="Replace with">${matchCell(row)}</td>
        <td data-label="Brings over">${carriesCell(row)}</td>
        <td class="num rs-acts">${st === 'one' ? `<button class="btn btn-sm btn-primary" type="button" data-act="accept" data-cid="${old.id}">Accept</button>` : ''}</td>
      </tr>`;
      return h;
    }).join('');
    applyFilter();
  }

  function visibleRows() {
    return filterBar ? rows.filter((r) => filterBar.matches(r)) : rows;
  }

  function applyFilter() {
    const vis = new Set(visibleRows().map((r) => r.old_id));
    document.querySelectorAll('#rs-tbody tr[data-cid]').forEach((tr) => { tr.hidden = !vis.has(Number(tr.dataset.cid)); });
    document.querySelectorAll('#rs-tbody tr.rs-acct').forEach((tr) => {
      let n = tr.nextElementSibling;
      let any = false;
      while (n && !n.classList.contains('rs-acct')) { if (!n.hidden) any = true; n = n.nextElementSibling; }
      tr.hidden = !any;
    });
    const shown = visibleRows();
    byId('rs-count').textContent = shown.length === rows.length ? counted(rows.length, 'channel')
      : `${nf(shown.length)} of ${counted(rows.length, 'channel')}`;
    const n = (s) => shown.filter((r) => rowState(r) === s).length;
    const all = document.querySelector('[data-act="accept-all"]');
    all.textContent = `Accept all ${nf(n('one'))}`;
    all.disabled = !n('one');
    const groups = new Set(shown.flatMap((r) => r.groups));
    byId('rs-stats').textContent = `${nf(n('one'))} ready · ${nf(n('several'))} need a choice · ${nf(n('none'))} no match · in ${counted(groups.size, 'group')}`;
    [...picks].forEach((id) => { if (!vis.has(id)) picks.delete(id); });
    updateBar();
  }

  function updateBar() {
    const btn = document.querySelector('[data-act="accept-selected"]');
    btn.disabled = !picks.size;
    btn.textContent = picks.size ? `Accept ${nf(picks.size)} selected` : 'Accept selected';
  }

  function wireFilters() {
    filterBar = createFilterBar({
      chipsEl: byId('rs-filter-chips'),
      menuEl: byId('rs-filter-menu'),
      dims: [
        { k: 'state', label: 'Replacement', values: [
          { v: 'one', label: 'Ready' }, { v: 'several', label: 'Needs a choice' }, { v: 'none', label: 'Not found' }],
          match: (r, v) => rowState(r) === v },
        { k: 'group', label: 'Group',
          values: () => [...new Set(rows.flatMap((r) => r.groups))]
            .sort((a, b) => groupName(a).localeCompare(groupName(b)))
            .map((g) => ({ v: String(g), label: groupName(g) })),
          match: (r, v) => r.groups.some((g) => String(g) === v) },
        { k: 'account', label: 'Account',
          values: () => [...new Set(rows.map((r) => ch(r.old_id).account_id))]
            .map((id) => ({ v: String(id), label: acct(id).name })),
          match: (r, v) => String(ch(r.old_id).account_id) === v },
      ],
      rows: () => rows,
      note: 'Choices inside one filter are an "or"; different filters are an "and".',
      onChange: applyFilter,
    });
    if (boot.group && rows.some((r) => r.groups.includes(boot.group))) {
      filterBar.setFrom([['group', String(boot.group)]]);
    }
    filterBar.render();
  }

  // ── Accepting ─────────────────────────────────────────────────────────
  function send(list) {
    const pairs = list.map((row) => {
      const p = pickOf(row);
      return { old_id: row.old_id, new_id: p.id, picked: !!p.picked };
    });
    if (!pairs.length) return Promise.resolve();
    if (pairs.length > 1) showToast(`Replacing ${counted(pairs.length, 'channel')}...`, { durationMs: 600000 });
    return jsonFetch('/api/replacements/accept', {
      method: 'POST',
      body: JSON.stringify({ pairs, carry: carryOn() }),
    }).then((d) => {
      hideToast();
      const gone = new Set(d.replaced.map((r) => r.old_id));
      rows = rows.filter((r) => !gone.has(r.old_id));
      // Every refusal stays on its own row with the server's reason, not only the first.
      d.skipped.forEach((s) => skipped.set(s.old_id, s.reason));
      gone.forEach((id) => { picks.delete(id); handPicked.delete(id); chosen.delete(id); });
      // The menu's count (base.html) is the number of rows this page still lists.
      document.querySelectorAll('.nav-count-replacements').forEach((el) => {
        el.textContent = rows.length;
        el.style.display = rows.length ? '' : 'none';
      });
      draw();
      if (d.replaced.length === 1 && !d.skipped.length) {
        const r = d.replaced[0];
        showToast(`Replaced: ${r.summary}.`);
      } else {
        const skip = d.skipped.length ? `; ${nf(d.skipped.length)} not replaced, each row says why` : '';
        showToast(`Replaced ${counted(d.replaced.length, 'channel')}${skip}.`,
          { type: d.skipped.length ? 'warning' : 'success', durationMs: d.skipped.length ? 12000 : 5000 });
      }
    }).catch((e) => {
      hideToast();
      if (pairs.length === 1 && e.status === 409) {
        skipped.set(pairs[0].old_id, e.message);
        draw();
      }
      showToast(e.message || 'The replacement did not go through.', { type: 'error', durationMs: 10000 });
    });
  }
  const rowOf = (cid) => rows.find((r) => r.old_id === cid);

  // ── Pick a channel by hand: the channel search, one query per keystroke pause ──────
  function openPicker(cid) {
    const old = ch(cid);
    let seq = 0;
    let found = [];
    const body = document.createElement('div');
    body.innerHTML = `<p class="small">Nothing listed carries the same stream as <b>${escHtml(old.name)}</b>. Search for the channel that took its place. Only you can say the two are the same, so it is recorded as picked by hand.</p>
      <input class="form-control" type="search" id="rs-pick-q" placeholder="Channel name" value="${escHtml(old.name)}">
      <ul class="rs-pick-list" id="rs-pick-list"><li class="text-faint small">Searching...</li></ul>`;
    let choice = null;
    const modal = buildModal({
      title: 'Pick a channel',
      body,
      footer: [
        { label: 'Cancel', class: 'btn' },
        { label: 'Use this channel', class: 'btn btn-primary', onClick: (close) => {
          if (!choice) { showToast('Choose a channel first.', { type: 'warning' }); return false; }
          handPicked.set(cid, choice);
          close();
          draw();
          return true;
        } },
      ],
    });
    const list = modal.querySelector('#rs-pick-list');
    const run = () => {
      const q = modal.querySelector('#rs-pick-q').value.trim();
      const mine = ++seq;
      const p = new URLSearchParams({ grain: 'channels', per_page: '25', counts: '0' });
      if (q) p.set('q', q);
      fetch(`/api/channels/search?${p.toString()}`).then((r) => r.json()).then((d) => {
        if (mine !== seq) return;
        found = (d.success && d.rows ? d.rows : []).filter((r) => r.id !== cid && r.kind !== 'group');
        list.innerHTML = found.length ? found.map((r) => `<li><label><input type="radio" name="rs-pick" value="${r.id}">
          <span>${escHtml(r.name)} <span class="text-faint small">${escHtml((r.account && r.account.name) || '')}</span></span></label></li>`).join('')
          : '<li class="text-faint small">No channel matches.</li>';
      }).catch(() => {
        if (mine === seq) list.innerHTML = '<li class="text-faint small">The search did not answer. Try again.</li>';
      });
    };
    let timer = null;
    modal.querySelector('#rs-pick-q').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 300); });
    list.addEventListener('change', (e) => {
      const r = found.find((x) => String(x.id) === e.target.value);
      if (r) choice = { id: r.id, name: r.name, account: (r.account && r.account.name) || '' };
    });
    run();
  }

  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-act]');
    if (!el || !byId('rs-main').contains(el)) return;
    const cid = Number(el.dataset.cid);
    switch (el.dataset.act) {
      case 'accept': send([rowOf(cid)]); break;
      case 'accept-selected': send(rows.filter((r) => picks.has(r.old_id) && rowState(r) === 'one')); break;
      case 'accept-all': send(visibleRows().filter((r) => rowState(r) === 'one')); break;
      case 'rechoose': chosen.delete(cid); picks.delete(cid); draw(); break;
      case 'pick': openPicker(cid); break;
      default: break;
    }
  });

  document.addEventListener('change', (e) => {
    const el = e.target;
    if (!byId('rs-main') || !byId('rs-main').contains(el)) return;
    if (el.id === 'rs-target') {
      target = el.value;
      picks.clear();
      draw();
    } else if (el.dataset.carry) {
      draw();
    } else if (el.dataset.act === 'choose') {
      chosen.set(Number(el.dataset.cid), Number(el.value));
      draw();
    } else if (el.dataset.act === 'pick-row') {
      const cid = Number(el.dataset.cid);
      if (el.checked) picks.add(cid); else picks.delete(cid);
      el.closest('tr').classList.toggle('rs-picked', el.checked);
      updateBar();
    } else if (el.dataset.act === 'select-all') {
      document.querySelectorAll('#rs-tbody tr[data-cid]:not([hidden]) input[data-act="pick-row"]').forEach((cb) => {
        cb.checked = el.checked;
        const cid = Number(cb.dataset.cid);
        if (el.checked) picks.add(cid); else picks.delete(cid);
        cb.closest('tr').classList.toggle('rs-picked', el.checked);
      });
      updateBar();
    }
  });

  sortRows();
  wireFilters();
  draw();
})();
