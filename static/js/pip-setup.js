/* The setup dialog of a picture-in-picture channel (the IPTV Output page, mockup 63,
   dev/docs/DESIGN-pip-channels.md 12). openPipSetup(spec) opens it on a channel's stored setup,
   or on nothing to create one; nothing is saved until Create or Save.

   The dialog has two views in one body: the main view, and a picture's picker in its place, so
   the body is the one thing that scrolls. A picker is a step: its header shows a back arrow
   where the X was, and back, a backdrop click and Escape all return to the main view.

   The pictures drawn here are stand-ins naming what each window will show, never live frames:
   a live frame would open a connection to draw a dialog. */
(() => {
  const BOOT = readBootJson('pip-boot', { boxes: {}, corners: {}, sizes: {}, name_max: 40, query_max: 80 });
  const TITLES = { main: 'Big picture', inset: 'Small picture' };
  const TABS = [['game', 'A game'], ['channel', 'A channel or group'], ['search', 'A search']];
  const DAYS = [['all', 'Any day'], ['now', 'On now'], ['today', 'Today'], ['tomorrow', 'Tomorrow'],
    ['week', 'Next 7 days'], ['later', 'Later']];
  // The most rows a picker draws at once; a search narrows the rest.
  const LIST_CAP = 200;
  const tabOf = (src) => (src && src.kind === 'group' ? 'channel' : src ? src.kind : 'game');

  function dayMatch(g, k) {
    if (k === 'all') return true;
    if (k === 'now') return g.live;
    if (k === 'today') return g.days === 0;
    if (k === 'tomorrow') return g.days === 1;
    if (k === 'week') return g.days < 7;
    if (k === 'later') return g.days >= 7;
    throw new Error(`unnamed day filter ${k}`);
  }

  // Two pictures that are plainly the same thing. Whether a game and a channel would play the
  // same channel right now is the server's to say, at Create and Save.
  function sameSource(a, b) {
    if (!a || !b || a.kind !== b.kind) return false;
    if (a.kind === 'search') return a.query.trim().toLowerCase() === b.query.trim().toLowerCase();
    return a.ref === b.ref;
  }

  // The box of each window in the stand-in picture, from the server's own geometry.
  const insetBox = (d) => (d.layout === 'side' ? BOOT.boxes.side : BOOT.boxes[d.corner + d.size]);

  async function openPipSetup(spec, { onSaved } = {}) {
    const creating = !spec;
    const lists = await jsonFetch('/api/iptv-output/pip/pickers');
    const games = lists.games;
    const channels = lists.channels;
    const leagues = [...new Set(games.map((g) => g.league))].sort();
    const s = spec || { name: '', main: null, inset: null, layout: 'corner', corner: 'br', size: 'm', swapped_copy: false };
    const wasSet = !!(s.main && s.inset);
    const d = {
      name: s.name, main: s.main ? { ...s.main } : null, inset: s.inset ? { ...s.inset } : null,
      layout: s.layout, corner: s.corner, size: s.size, twin: s.swapped_copy,
      tab: { main: tabOf(s.main), inset: tabOf(s.inset) },
      filter: { main: '', inset: '' },
      q: { main: s.main && s.main.kind === 'search' ? s.main.query : '', inset: s.inset && s.inset.kind === 'search' ? s.inset.query : '' },
      gq: { main: '', inset: '' }, league: { main: '', inset: '' }, day: { main: 'all', inset: 'all' },
      view: null,   // 'main' or 'inset' while that picture's picker is open
    };
    const wrap = document.createElement('div');
    wrap.className = 'pip-dlg';
    let el = null;
    let saving = false;

    const searchHits = (q) => {
      const n = String(q || '').trim().toLowerCase();
      return n ? channels.filter((c) => c.name.toLowerCase().includes(n)) : [];
    };
    // What a picture is called: from the lists when it is in them, else the words its stored
    // setup came with (a game that is over, a channel that left the TV Guide).
    function srcLabel(src) {
      if (src.kind === 'game') {
        const g = games.find((x) => x.ref === src.ref);
        return g ? { t: `${g.league}: ${g.title}`, s: g.label_sub, short: g.title }
          : { t: src.title || 'A game', s: src.sub || '', short: src.short || 'A game' };
      }
      if (src.kind === 'channel' || src.kind === 'group') {
        const c = channels.find((x) => x.kind === src.kind && x.ref === src.ref);
        return c ? { t: c.name, s: c.sub, short: c.name }
          : { t: src.title || 'A channel', s: src.sub || '', short: src.short || 'A channel' };
      }
      if (src.kind === 'search') {
        const hits = searchHits(src.query);
        return { t: `Channels named "${src.query}"`, short: `"${src.query}"`,
          s: hits.length ? `A search: ${counted(hits.length, 'channel')} ${hits.length === 1 ? 'matches' : 'match'} now` : 'A search: nothing matches right now' };
      }
      throw new Error(`unnamed kind of picture ${src.kind}`);
    }

    function problem() {
      if (!d.main) return 'Choose what the big picture shows.';
      if (!d.inset) return 'Choose what the small picture shows.';
      if (sameSource(d.main, d.inset)) return 'The two pictures would play the same channel. Choose something else for one of them.';
      if (!d.name.trim()) return 'Give it a name.';
      return '';
    }

    function schematicHtml() {
      const ph = (src, which, small) => {
        const l = src ? srcLabel(src) : null;
        return `<div class="pip-ph${small ? ' pip-ph-small' : ''}"><div class="pip-ph-k">${which}</div>`
          + `<div class="pip-ph-t" title="${escHtml(l ? l.t : '')}">${escHtml(l ? l.short : 'Not chosen yet')}</div></div>`;
      };
      const side = d.layout === 'side';
      const half = side ? ' pip-half' : '';
      return `<div class="pip-tv"><div class="pip-pane${half}"${side ? ` style="${BOOT.boxes.side_main}"` : ''}>${ph(d.main, 'Big picture', false)}</div>`
        + `<div class="pip-pane pip-inset${half}" style="${insetBox(d)}">${ph(d.inset, 'Small picture', true)}</div></div>`;
    }

    const capped = (rows, html) => (rows.length > LIST_CAP
      ? `${html}<p class="card-note">Showing the first ${LIST_CAP} of ${rows.length}. Search to narrow it.</p>` : html);

    function gameList(which) {
      const cur = d[which];
      const other = d[which === 'main' ? 'inset' : 'main'];
      const q = d.gq[which].trim().toLowerCase();
      const found = games.filter((g) => !d.league[which] || g.league === d.league[which])
        .filter((g) => dayMatch(g, d.day[which]))
        .filter((g) => !q || `${g.league} ${g.title}`.toLowerCase().includes(q));
      let lastDay = null;
      const rows = found.slice(0, LIST_CAP).map((g) => {
        const on = cur && cur.kind === 'game' && cur.ref === g.ref;
        const taken = other && other.kind === 'game' && other.ref === g.ref;
        const head = g.day !== lastDay ? `<div class="pip-day">${escHtml(g.day)}</div>` : '';
        lastDay = g.day;
        return `${head}<label class="pip-opt${taken ? ' pip-dis' : ''}"><input type="radio" name="pip-${which}" data-pick="${which}" data-k="game" data-ref="${g.ref}"${on ? ' checked' : ''}${taken ? ' disabled' : ''}>
          <span><span class="t">${escHtml(g.title)}</span><span class="s">${escHtml(g.sub)}${taken ? '. Already the other picture' : ''}</span></span></label>`;
      }).join('');
      const none = games.length ? 'No game matches. Clear the search or pick another day or league.'
        : 'No coming game is in a league you follow. Follow a league or a team in Sports Schedules and its games show up here.';
      return `<div class="toolbar pip-gtools">
          <input class="search" type="search" placeholder="Search teams, races, leagues" data-gq="${which}" value="${escHtml(d.gq[which])}" aria-label="Search games">
          <select class="form-control" data-league="${which}" aria-label="League"><option value="">Every league you follow</option>${leagues.map((x) => `<option${d.league[which] === x ? ' selected' : ''}>${escHtml(x)}</option>`).join('')}</select>
        </div>
        <div class="pip-chips">${DAYS.map(([k, l]) => `<button type="button" class="chip${d.day[which] === k ? ' active' : ''}" data-day="${which}" data-k="${k}">${l}<span class="n">${games.filter((g) => dayMatch(g, k)).length}</span></button>`).join('')}</div>
        <div class="pip-list">${capped(found, rows) || `<p class="card-note">${none}</p>`}</div>
        <p class="card-note">Every coming game in the leagues you follow, as far ahead as their schedules go. A game comes on here ahead of its start and stays on after its listing ends, like the sports channels. Your TV Guide covers ${counted(lists.guide_days, 'day')}, so a game further out has no channel yet: it gets one by itself once a guide lists it.</p>`;
    }

    function list(which) {
      const tab = d.tab[which];
      const cur = d[which];
      if (tab === 'game') return gameList(which);
      if (tab === 'channel') {
        const f = d.filter[which].trim().toLowerCase();
        const chs = channels.filter((c) => !f || c.name.toLowerCase().includes(f));
        const rows = chs.slice(0, LIST_CAP).map((c) => {
          const on = cur && cur.kind === c.kind && cur.ref === c.ref;
          return `<label class="pip-opt"><input type="radio" name="pip-${which}" data-pick="${which}" data-k="${c.kind}" data-ref="${c.ref}"${on ? ' checked' : ''}>
            <span><span class="t">${escHtml(c.name)}</span><span class="s">${escHtml(c.sub)}</span></span></label>`;
        }).join('');
        return `<input class="search" type="search" placeholder="Search your TV Guide's channels" data-filter="${which}" value="${escHtml(d.filter[which])}" aria-label="Search channels">
          <div class="pip-list">${capped(chs, rows) || '<p class="card-note">No channel in your TV Guide matches.</p>'}</div>`;
      }
      if (tab === 'search') {
        const hits = searchHits(d.q[which]);
        const rows = hits.slice(0, LIST_CAP).map((c) => `<div class="pip-opt"><span><span class="t">${escHtml(c.name)}</span><span class="s">${escHtml(c.sub)}</span></span></div>`).join('');
        return `<input class="search" type="search" placeholder="Part of a channel's name, like in-car" data-q="${which}" value="${escHtml(d.q[which])}" maxlength="${BOOT.query_max}" aria-label="Channel name contains">
          <p class="card-note">Plays the first channel whose name has this in it, in TV Guide order, on an account with a connection free. It is asked again every time a TV tunes in, so a channel your provider adds later is found too.</p>
          <div class="pip-list">${capped(hits, rows)
            || `<p class="card-note">${d.q[which].trim() ? 'No channel matches right now. It plays the first one that does.' : 'Type part of a channel name.'}</p>`}</div>`;
      }
      throw new Error(`unnamed picker tab ${tab}`);
    }

    // The main view names each picture in one row; choosing opens that picture's picker in
    // place of the main view.
    const wire = (src) => ({ kind: src.kind, ref: src.kind === 'search' ? null : src.ref, query: src.kind === 'search' ? src.query : '' });

    // What each picture would start from right now and the connections that takes, asked of
    // the server for the draft as it stands: only it knows which accounts are busy.
    const would = { key: '', main: null, inset: null, seats: '' };
    function wouldText(which) {
      const w = would[which];
      if (!w) return '';
      // A picture that is one channel already names it: only the account is news.
      const named = d[which] && srcLabel(d[which]).t === w.channel;
      return named ? `Would play from ${w.account}` : `Would play from ${w.channel} on ${w.account}`;
    }
    function showWould() {
      ['main', 'inset'].forEach((which) => {
        const node = wrap.querySelector(`[data-would="${which}"]`);
        if (node) node.textContent = wouldText(which);
      });
      const seats = wrap.querySelector('[data-pip-seats]');
      if (seats) { seats.textContent = would.seats; seats.hidden = !would.seats; }
    }
    function askWould() {
      const body = JSON.stringify({ main: d.main ? wire(d.main) : null, inset: d.inset ? wire(d.inset) : null });
      if (body === would.key) return;
      would.key = body;
      if (!d.main && !d.inset) { Object.assign(would, { main: null, inset: null, seats: '' }); showWould(); return; }
      jsonFetch('/api/iptv-output/pip/would-play', { method: 'POST', body })
        .then((got) => {
          if (body !== would.key) return;      // the draft moved on while this was asked
          Object.assign(would, { main: got.main, inset: got.inset, seats: got.seats });
          showWould();
        })
        // The lines are a help, not the dialog: without them it still saves.
        .catch(() => { if (body === would.key) { Object.assign(would, { key: '', main: null, inset: null, seats: '' }); showWould(); } });
    }

    function pickRow(which) {
      const l = d[which] ? srcLabel(d[which]) : null;
      return `<button type="button" class="pip-pickrow" data-open="${which}">
          <span class="pip-pr-k">${TITLES[which]}</span>
          <span class="pip-pr-v">${l ? `<span class="t">${escHtml(l.t)}</span><span class="s">${escHtml(l.s)}</span><span class="s" data-would="${which}">${escHtml(wouldText(which))}</span>` : '<span class="sp-none">Not chosen yet</span>'}</span>
          <span class="pip-pr-a">${l ? 'Change' : 'Choose'}${icon('forward')}</span></button>`;
    }

    function pickerView(which) {
      const useSearch = d.tab[which] === 'search'
        ? `<div class="pip-use"><button type="button" class="btn btn-primary" data-dlg="use-search"${d.q[which].trim() ? '' : ' disabled'}>Use this search</button></div>` : '';
      return `<div class="tabs" role="tablist">${TABS.map(([k, l]) => `<button type="button" class="tab${d.tab[which] === k ? ' active' : ''}" role="tab" aria-selected="${d.tab[which] === k}" data-tab="${which}" data-k="${k}">${l}</button>`).join('')}</div>
        <div class="pip-src" data-src="${which}">${list(which)}</div>${useSearch}`;
    }

    function setFoot(why) {
      if (!el) return;
      if (el.footNote) el.footNote.textContent = why;
      const save = el.querySelector('.modal-foot .btn-primary');
      save.classList.toggle('disabled', !!why);
      save.setAttribute('aria-disabled', why ? 'true' : 'false');
    }

    function draw(focus) {
      const picking = d.view;
      if (el) {
        el.querySelector('.modal-foot').hidden = !!picking;
        const h2 = el.querySelector('.modal-head h2');
        h2.textContent = picking ? `${TITLES[picking]}: choose what it shows` : h2.dataset.title;
        el.querySelector('.modal-close').hidden = !!picking;
        el.querySelector('.modal-back').hidden = !picking;
        el.escapeHandler = picking ? closePicker : null;
        el.backdropHandler = picking ? closePicker : null;
      }
      if (picking) {
        wrap.innerHTML = pickerView(picking);
      } else {
        const games2 = [d.main, d.inset].some((x) => x && x.kind === 'game');
        const side = d.layout === 'side';
        const chip = (attr, val, cur, label, dis) => `<button type="button" class="chip${cur === val ? ' active' : ''}" data-${attr}="${val}"${dis ? ' aria-disabled="true"' : ''}>${escHtml(label)}</button>`;
        const shown = (d.name || s.name || 'It').trim();
        wrap.innerHTML = `<div class="pip-top">
            <div>${schematicHtml()}</div>
            <div class="pip-opts">
              <div><label class="pip-row-l" for="pip-name">Name in the IPTV player</label><input class="form-control" id="pip-name" value="${escHtml(d.name)}" maxlength="${BOOT.name_max}"></div>
              <div><div class="pip-row-l">Layout</div><div class="pip-chips">${chip('layout', 'corner', d.layout, 'Small picture in a corner')}${chip('layout', 'side', d.layout, 'Side by side')}</div></div>
              <div><div class="pip-row-l">Corner</div><div class="pip-chips">${Object.entries(BOOT.corners).map(([k, l]) => chip('corner', k, side ? '' : d.corner, l, side)).join('')}</div></div>
              <div><div class="pip-row-l">Size of the small picture</div><div class="pip-chips">${Object.entries(BOOT.sizes).map(([k, l]) => chip('size', k, side ? '' : d.size, l, side)).join('')}</div></div>
            </div></div>
          <div class="pip-picks">${pickRow('main')}${pickRow('inset')}
            <button type="button" class="btn btn-sm pip-swap" data-dlg="swap"${d.main || d.inset ? '' : ' disabled'}>Swap the two pictures</button></div>
          <div class="pip-twin"><label class="switch"><input type="checkbox" data-dlg="twin"${d.twin ? ' checked' : ''} aria-label="Also offer it swapped"><span class="knob"></span></label>
            <div><div class="pip-twin-name">Also offer it swapped</div><div class="card-note">Adds "${escHtml(shown)} (swapped)" right after it in the IPTV player, with the small picture big. Changing between the two keeps both connections, so it is quick.</div></div></div>
          ${games2 ? '<p class="card-note">A game that is over says so in its picture until you change it.</p>' : ''}
          <p class="card-note" data-pip-seats${would.seats ? '' : ' hidden'}>${escHtml(would.seats)}</p>`;
        askWould();
      }
      setFoot(problem());
      if (focus) {
        const f = wrap.querySelector(focus);
        if (f) { f.focus(); const v = f.value; f.value = ''; f.value = v; }
      }
    }
    function toTop() {
      const body = el && el.querySelector('.modal-body');
      if (body) body.scrollTop = 0;
    }
    function openPicker(which) { d.view = which; draw(); toTop(); }
    function closePicker() { d.view = null; draw(); toTop(); }

    wrap.addEventListener('click', (e) => {
      const r = e.target.closest('input[data-pick]');
      if (r && d.view) {
        // Clicking the picture already chosen is a choice too: back to the main view.
        const cur = d[r.dataset.pick];
        if (cur && cur.kind === r.dataset.k && cur.ref === Number(r.dataset.ref)) { closePicker(); return; }
      }
      const t = e.target.closest('[data-tab],[data-layout],[data-corner],[data-size],[data-day],[data-open],[data-dlg="swap"],[data-dlg="use-search"]');
      if (!t || t.getAttribute('aria-disabled') === 'true' || t.disabled) return;
      if (t.dataset.open) { openPicker(t.dataset.open); return; }
      if (t.dataset.dlg === 'use-search') { closePicker(); return; }
      if (t.dataset.tab) d.tab[t.dataset.tab] = t.dataset.k;
      else if (t.dataset.day) d.day[t.dataset.day] = t.dataset.k;
      else if (t.dataset.layout) d.layout = t.dataset.layout;
      else if (t.dataset.corner) d.corner = t.dataset.corner;
      else if (t.dataset.size) d.size = t.dataset.size;
      else if (t.dataset.dlg === 'swap') {
        [d.main, d.inset] = [d.inset, d.main];
        ['tab', 'q', 'gq', 'league', 'day', 'filter'].forEach((k) => { [d[k].main, d[k].inset] = [d[k].inset, d[k].main]; });
      }
      draw();
    });
    wrap.addEventListener('change', (e) => {
      const t = e.target;
      if (t.dataset.pick) { d[t.dataset.pick] = { kind: t.dataset.k, ref: Number(t.dataset.ref), query: '' }; closePicker(); }
      else if (t.dataset.league !== undefined) { d.league[t.dataset.league] = t.value; draw(); }
      else if (t.dataset.dlg === 'twin') d.twin = t.checked;
    });
    wrap.addEventListener('input', (e) => {
      const t = e.target;
      if (t.id === 'pip-name') { d.name = t.value; setFoot(problem()); return; }
      if (t.dataset.filter) { d.filter[t.dataset.filter] = t.value; draw(`[data-filter="${t.dataset.filter}"]`); }
      if (t.dataset.gq) { d.gq[t.dataset.gq] = t.value; draw(`[data-gq="${t.dataset.gq}"]`); }
      if (t.dataset.q) {
        const w = t.dataset.q;
        d.q[w] = t.value;
        d[w] = t.value.trim() ? { kind: 'search', ref: null, query: t.value.trim() } : null;
        draw(`[data-q="${w}"]`);
      }
    });

    el = buildModal({
      title: creating ? 'Create a PiP channel' : wasSet ? `Change ${s.name}` : `Set up ${s.name}`,
      body: wrap, panelClass: 'modal-xwide', footNote: ' ',
      footer: [{ label: 'Cancel' }, { label: creating ? 'Create' : 'Save', class: 'btn btn-primary', onClick: (close) => {
        const why = problem();
        if (why) { showToast(why, { type: 'error' }); return false; }
        if (saving) return false;
        saving = true;
        const body = { name: d.name.trim(), main: wire(d.main), inset: wire(d.inset), layout: d.layout,
          corner: d.corner, size: d.size, swapped_copy: d.twin };
        jsonFetch(creating ? '/api/iptv-output/pip' : `/api/iptv-output/pip/${s.id}`, { method: 'POST', body: JSON.stringify(body) })
          .then((saved) => { close(); if (onSaved) onSaved(saved, creating); })
          .catch((err) => { saving = false; showToast(err.message || 'That did not work.', { type: 'error' }); });
        return false;
      } }],
    });
    const h2 = el.querySelector('.modal-head h2');
    h2.dataset.title = h2.textContent;
    h2.insertAdjacentHTML('beforebegin', `<button type="button" class="modal-back" aria-label="Back to ${escHtml(h2.textContent)}" hidden>${icon('back')}</button>`);
    el.querySelector('.modal-back').addEventListener('click', closePicker);
    draw();
    return el;
  }

  window.openPipSetup = openPipSetup;
})();
