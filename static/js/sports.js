/* The Sports Schedules pages (templates/sports.html, sports_league.html, mockup 50,
   dev/docs/DESIGN-sports-data.md 5 and 11).

   Add a league, Change teams, Stop following and Delete league, the listings dialog's Wrong match,
   and a league's names and categories (mockup 51), then a fresh server render
   (swapFromServer), so the templates stay the one renderer. A row and the league's action bar carry
   what the dialogs need as data attributes, which come back with that render. Clicks are delegated,
   since a swap rebuilds the nodes. The list sorts on its own column headers (DESIGN.md 10.3). */
(() => {
  const BOOT = readBootJson('sports-boot', { shipped: [], site: '' });
  const PAGE = window.SPORTS_LEAGUE || null;

  const siteLink = (text = 'TheSportsDB', path = '') =>
    `<a href="${escHtml(BOOT.site + path)}" target="_blank" rel="noopener">${escHtml(text)}</a>`;

  const refresh = () => {
    const regions = PAGE
      ? ['.gd-head', '.gd-statezone', '#sp-sections']
      : ['.page-head', '#sports-body'];
    return swapFromServer(regions).then(applySort).catch(() => window.location.reload());
  };

  const dialogOf = (el) => {
    const holder = el.closest('[data-dialog]') || document.getElementById('sp-actionbar');
    return holder ? JSON.parse(holder.dataset.dialog) : null;
  };

  /* ---- Sorting --------------------------------------------------------------------------- */
  const SORT_LABELS = { name: 'League', status: 'Status', next: 'Next game', count: 'Upcoming', updated: 'Updated' };
  const SORT_NUMERIC = new Set(['status', 'count', 'updated']);
  const SORT_FIRST = { name: 'asc', status: 'asc', next: 'asc', count: 'desc', updated: 'asc' };
  const sort = { key: 'next', dir: 'asc' };

  function applySort() {
    const rows = document.getElementById('sp-rows');
    if (!rows) return;
    sortChildren(rows, '.row', (el) => {
      const raw = el.dataset[sort.key];
      return SORT_NUMERIC.has(sort.key) ? Number(raw) : raw;
    }, sort.dir);
    document.querySelectorAll('#sp-head [data-sort]').forEach((cell) => {
      const on = cell.dataset.sort === sort.key;
      cell.classList.toggle('sorted', on);
      cell.querySelector('.sort-ind').innerHTML = on ? icon(sort.dir === 'asc' ? 'sort-asc' : 'sort-desc') : '';
    });
    const chip = document.getElementById('sp-sort-chip');
    if (chip) chip.innerHTML = `Sort: ${escHtml(SORT_LABELS[sort.key])} ${icon('dropdown')}`;
  }

  function sortBy(key) {
    sort.dir = sort.key === key ? (sort.dir === 'asc' ? 'desc' : 'asc') : SORT_FIRST[key];
    sort.key = key;
    applySort();
  }

  /* ---- Add a league ---------------------------------------------------------------------- */
  function listHtml(needle) {
    const q = needle.trim().toLowerCase();
    const rows = BOOT.shipped.filter((l) => !q || l.name.toLowerCase().includes(q) || l.sport.toLowerCase().includes(q));
    const picks = rows.map((l) => `<button type="button" class="sp-pick" data-pick="${l.tsdb_id}"${l.followed ? ' disabled' : ''}>
        <span>${escHtml(l.name)}</span><span class="meta">${l.followed ? 'Following' : escHtml(l.sport)}</span></button>`).join('');
    const lookup = q && !rows.length
      ? `<button type="button" class="btn btn-sm sp-lookup" data-lookup="1">Look up on TheSportsDB</button>
         <span class="form-hint">Finds a team and its league, or checks a league's number or link. One call.</span>`
      : '';
    const hint = q ? '' : `<span class="form-hint sp-lookup">Not listed? Type a team name, a league's number, or paste a link to its page on ${siteLink()}.</span>`;
    return `<div class="sp-pick-list">${picks}</div>${lookup}${hint}`;
  }

  function openAdd() {
    const body = document.createElement('div');
    body.innerHTML = `<div class="form-group sp-entry-group">
        <label for="sp-entry">League, team or link</label>
        <input id="sp-entry" class="form-control" type="text" autocomplete="off" placeholder="NFL, Boston Bruins, or a link">
      </div>
      <div id="sp-results">${listHtml('')}</div><div id="sp-msg"></div>`;
    const modal = buildModal({ title: 'Add a league', body, footer: [{ label: 'Cancel' }] });
    const input = body.querySelector('#sp-entry');
    const results = body.querySelector('#sp-results');
    const msg = body.querySelector('#sp-msg');
    const setMsg = (text, cls = 'bad') => {
      msg.innerHTML = text ? `<div class="sp-msg ${cls}">${escHtml(text).replace('its page on TheSportsDB', `its page on ${siteLink()}`)}</div>` : '';
    };

    function showConfirm(league, team) {
      const leaguePage = `league/${league.tsdb_id}`;
      body.innerHTML = `<div class="sp-confirm-name">${escHtml(league.name)}</div>
        <div class="sp-confirm-sub">${[league.sport, league.season ? `${league.season} season` : null].filter(Boolean).map(escHtml).join(', ')}${league.sport || league.season ? ', ' : ''}${siteLink(`TheSportsDB league ${league.tsdb_id}`, leaguePage)}</div>
        ${team ? `<div class="form-group sp-follow-group">
          <label class="sp-radio"><input type="radio" name="sp-follow" value="team" checked> Only ${escHtml(team.team)}</label>
          <label class="sp-radio"><input type="radio" name="sp-follow" value="all"> All games in the league</label>
          <span class="form-hint">Following one team costs the same calls as its whole league. More teams can be picked once its schedule is here.</span>
        </div>` : ''}
        <div class="sp-msg">Getting a new schedule can take up to an hour, because the calls are spread out. After that it costs a few calls a day.</div>
        <div id="sp-msg-2"></div>`;
      const foot = modal.querySelector('.modal-foot');
      foot.innerHTML = '';
      const cancel = document.createElement('button');
      cancel.className = 'btn';
      cancel.textContent = 'Cancel';
      cancel.addEventListener('click', () => modal.closeModal());
      const go = document.createElement('button');
      go.className = 'btn btn-primary';
      go.textContent = 'Follow';
      go.addEventListener('click', async () => {
        const choice = body.querySelector('input[name="sp-follow"]:checked');
        const teamId = team && choice && choice.value === 'team' ? team.team_id : null;
        go.disabled = true;
        try {
          await jsonFetch('/api/sports/leagues', {
            method: 'POST', body: JSON.stringify({ tsdb_id: league.tsdb_id, team_id: teamId }),
          });
          modal.closeModal();
          showToastAfterLoad(`Following ${league.name}. Getting its schedule.`);
          window.location.reload();
        } catch (err) {
          go.disabled = false;
          body.querySelector('#sp-msg-2').innerHTML = `<div class="sp-msg bad">${escHtml(err.message)}</div>`;
        }
      });
      foot.append(cancel, go);
    }

    async function lookup(payload) {
      setMsg('');
      let answer;
      try {
        answer = await jsonFetch('/api/sports/lookup', { method: 'POST', body: JSON.stringify(payload) });
      } catch (err) {
        setMsg(err.message, err.status === 409 ? 'warn' : 'bad');
        return;
      }
      if (answer.league) { showConfirm(answer.league, answer.team || null); return; }
      results.innerHTML = `<div class="sp-msg">Which one do you mean?</div><div class="sp-pick-list">${answer.choices.map((c, i) => `<button type="button" class="sp-pick" data-choice="${i}"><span>${escHtml(c.team)}</span><span class="meta">${escHtml(c.league.name)}</span></button>`).join('')}</div>`;
      results.onclick = (e) => {
        const b = e.target.closest('[data-choice]');
        if (!b) return;
        const c = answer.choices[Number(b.dataset.choice)];
        showConfirm(c.league, c);
      };
    }

    input.addEventListener('input', () => {
      setMsg('');
      results.onclick = null;
      results.innerHTML = listHtml(input.value);
    });
    input.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      if (results.querySelector('[data-lookup]')) lookup({ text: input.value });
    });
    body.addEventListener('click', (e) => {
      const pick = e.target.closest('[data-pick]');
      if (pick && !pick.disabled) { lookup({ tsdb_id: Number(pick.dataset.pick) }); return; }
      if (e.target.closest('[data-lookup]')) lookup({ text: input.value });
    });
    setTimeout(() => input.focus(), 0);
  }

  /* ---- Change teams ---------------------------------------------------------------------- */
  function openTeams(d) {
    const chosen = new Set(d.chosen);
    const body = document.createElement('div');
    const draw = (q) => d.teams.filter((t) => t.name.toLowerCase().includes(q.trim().toLowerCase()))
      .map((t) => `<label class="check-row"><input type="checkbox" value="${t.id}"${chosen.has(t.id) ? ' checked' : ''}> ${escHtml(t.name)}</label>`).join('')
      || '<div class="sp-msg">No team by that name in this schedule.</div>';
    body.innerHTML = `<div class="sp-confirm-sub sp-teams-note">This makes no calls. The whole league's schedule is already here, and this only picks which games are yours.</div>
      <label class="sp-radio"><input type="radio" name="sp-t" value="all"${d.whole ? ' checked' : ''}> All games</label>
      <label class="sp-radio"><input type="radio" name="sp-t" value="teams"${d.whole ? '' : ' checked'}> Only these teams</label>
      <input class="form-control sp-team-filter" id="sp-tf" type="text" placeholder="Filter teams" autocomplete="off">
      <div class="sp-teams" id="sp-tl">${draw('')}</div>`;
    buildModal({
      title: `${d.name}: teams`, body,
      footer: [{ label: 'Cancel' }, {
        label: 'Save', class: 'btn btn-primary',
        onClick: (close) => {
          const useTeams = body.querySelector('input[name="sp-t"]:checked').value === 'teams';
          if (useTeams && !chosen.size) {
            showToast('Pick at least one team, or choose All games.', { type: 'error' });
            return false;
          }
          jsonFetch(`/api/sports/leagues/${d.id}/following`, {
            method: 'PUT', body: JSON.stringify({ team_ids: useTeams ? [...chosen] : [] }),
          }).then(() => { close(); return refresh(); })
            .catch((err) => showToast(err.message, { type: 'error' }));
          return false;
        },
      }],
    });
    body.addEventListener('input', (e) => {
      if (e.target.id === 'sp-tf') body.querySelector('#sp-tl').innerHTML = draw(e.target.value);
    });
    body.addEventListener('change', (e) => {
      if (e.target.type !== 'checkbox') return;
      const id = Number(e.target.value);
      if (e.target.checked) chosen.add(id); else chosen.delete(id);
      body.querySelector('input[name="sp-t"][value="teams"]').checked = true;
    });
  }

  /* ---- Stop following, follow again, delete ---------------------------------------------- */
  const gamesWord = (n) => `${n} upcoming ${n === 1 ? 'game' : 'games'}`;

  async function stop(d) {
    const ok = await confirmModal({
      title: `Stop following ${d.name}?`,
      message: `ChannelBin stops checking TheSportsDB for ${d.name}'s games.`,
      consequence: `${d.stored ? `It keeps the ${gamesWord(d.stored)} it already has, and deletes each one ${d.keep_days} days after it's over. ` : ''}The league stays in this list so you can follow it again.`,
      confirmLabel: 'Stop following',
    });
    if (!ok) return;
    jsonFetch(`/api/sports/leagues/${d.id}/following`, { method: 'DELETE' }).then(refresh)
      .catch((err) => showToast(err.message, { type: 'error' }));
  }

  function refollow(d) {
    jsonFetch(`/api/sports/leagues/${d.id}/following`, { method: 'PUT', body: JSON.stringify({ team_ids: [] }) })
      .then(() => { showToast(`Following ${d.name} again.`); return refresh(); })
      .catch((err) => showToast(err.message, { type: 'error' }));
  }

  async function del(d) {
    const ok = await confirmModal({
      title: `Delete ${d.name}?`,
      message: `This deletes ${d.name} from ChannelBin, with its stored games and its team names.`,
      consequence: 'You can add the league again later, and its schedule will be fetched again.',
      confirmLabel: 'Delete league', danger: true,
    });
    if (!ok) return;
    jsonFetch(`/api/sports/leagues/${d.id}`, { method: 'DELETE' }).then(() => {
      if (PAGE) window.location.href = PAGE.listUrl; // nav-ok: the page this was on no longer exists
      else refresh();
    }).catch((err) => showToast(err.message, { type: 'error' }));
  }

  /* ---- Listings on your guide, Wrong match (app/sports_match.py, mockup 51) ---------------- */
  const leagueId = () => Number((document.getElementById('sp-actionbar') || {}).dataset.league);

  function groupHtml(grp, gi, open) {
    const wrongCount = grp.channels.filter((c) => c.wrong).length;
    const label = grp.sub ? `${grp.title}: ${grp.sub}` : grp.title;
    const count = `${grp.channels.length} ${grp.channels.length === 1 ? 'channel' : 'channels'}${wrongCount && !grp.all_wrong ? `, ${wrongCount} marked wrong` : ''}`;
    const act = grp.all_wrong
      ? `<span class="sp-rej">All marked wrong match</span><button class="btn btn-sm" data-wrong="0" data-group="${gi}">Undo</button>`
      : `<button class="btn btn-sm btn-danger" data-wrong="1" data-group="${gi}">Wrong match</button>`;
    const chans = open ? `<div class="sp-chans">${grp.channels.map((c) => {
      const btn = grp.all_wrong ? ''
        : c.wrong ? `<span class="sp-rej">Wrong match</span><button class="btn btn-sm" data-wrong="0" data-match="${c.id}">Undo</button>`
          : `<button class="btn btn-sm" data-wrong="1" data-match="${c.id}">Wrong match</button>`;
      return `<div class="sp-chan${c.wrong ? ' rejected' : ''}"><span class="sp-cname">${escHtml(c.name)}<span class="sp-cat">${escHtml(c.category)}</span></span><span class="sp-cact">${btn}</span></div>`;
    }).join('')}</div>` : '';
    return `<div class="sp-group${open ? ' open' : ''}${grp.all_wrong ? ' rejected' : ''}">
      <div class="sp-ghead">
        <button class="sp-gtoggle" data-toggle="${gi}" aria-expanded="${open}"><span class="sp-caret">${icon('expand')}</span><span class="t">${escHtml(label)}</span></button>
        <span class="sp-gmeta">${escHtml(grp.when)} <span class="badge ${grp.airing_label === 'Live' ? 'b-done' : 'b-abort'}">${escHtml(grp.airing_label)}</span></span>
      </div>
      <div class="sp-gline"><p class="sp-why"><strong>${count}</strong>. ${escHtml(grp.reason)}</p><span class="sp-gact">${act}</span></div>
      ${chans}</div>`;
  }

  /* Event channels (design 6.8): no real listing, only a name that names the event. The reason comes
     without its "Read from the channel's name" clause, which the heading's note says once. */
  function namesHtml(names, one) {
    if (!names.length) return '';
    const rows = names.map((n) => {
      const act = n.wrong
        ? `<span class="sp-rej">Wrong match</span><button class="btn btn-sm" data-wrong="0" data-match="${n.id}">Undo</button>`
        : `<button class="btn btn-sm" data-wrong="1" data-match="${n.id}">Wrong match</button>`;
      const badge = n.badge ? ` <span class="badge ${n.badge[0]}">${escHtml(n.badge[1])}</span>` : '';
      return `<div class="sp-chan sp-ename${n.wrong ? ' rejected' : ''}"><span class="sp-cname">${escHtml(n.name)}${badge}<span class="sp-cat">${escHtml(n.category)}</span><span class="sp-why">${escHtml(n.why)}</span></span><span class="sp-cact">${act}</span></div>`;
    }).join('');
    return `<h3 class="sp-h3">Event channels <span class="cnt">${names.length}</span></h3>
      <p class="sp-intro">These channels have no real listings in your guide: none at all, or only their own name repeated. Each one's name says it carries this ${one}, and the name is all ChannelBin has to go on. Providers rename these channels as the day goes on, so the names are read again after every sync, and a channel whose name moves on to another ${one} drops off this list.</p>
      <div class="sp-enames">${rows}</div>`;
  }

  function openListings(eventId, name, namesOnly) {
    let data = null;
    let changed = false;
    const opened = new Set();
    const modal = buildModal({
      title: namesOnly ? `${name}: event channels naming it` : `${name}: where it is in your guide`,
      panelClass: 'sp-dialog',
      body: '<div class="sp-dialog-body"><p class="sp-intro">Loading&hellip;</p></div>',
      footer: [{ label: 'Close', class: 'btn' }],
      onClose: () => { if (changed) refresh(); },
    });
    const body = modal.querySelector('.sp-dialog-body');
    const draw = () => {
      const foot = `<p class="sp-foot">Is it wrong on TheSportsDB itself, like its time or teams? <a href="${escHtml(data.tsdb_url)}" target="_blank" rel="noopener">Edit it on TheSportsDB ${icon('forward')}</a></p>`;
      if (!data.groups.length) {
        body.innerHTML = data.names.length
          ? `${namesHtml(data.names, data.one)}
            <p class="sp-intro sp-faint">If a name is wrong for this ${data.one}, press its <strong>Wrong match</strong>. Nothing is recorded, cancelled or deleted, and Undo puts it back. ChannelBin reads what the name says. It cannot see what the channel actually shows.</p>${foot}`
          : `<p class="sp-intro">Nothing in your guide matches it now.</p>${foot}`;
        return;
      }
      body.innerHTML = `<p class="sp-intro">These are the <strong>${data.listings} ${data.listings === 1 ? 'listing' : 'listings'}</strong> on <strong>${data.channels} ${data.channels === 1 ? 'channel' : 'channels'}</strong> in your guide that ChannelBin believes are this one, grouped by how the listing is worded. Open a group to see its channels.</p>
        <p class="sp-intro">If ChannelBin got a group wrong, press its <strong>Wrong match</strong>. Those listings then stop counting: this count drops, ChannelBin works out the live airing and the guide's time without them, and anything that later uses this match, such as a recording's details or an auto-record rule, leaves them out. Nothing is recorded, cancelled or deleted, and Undo puts it back. Inside a group, each channel has its own Wrong match for the rare channel whose listing is wrong when the rest are right.</p>
        <p class="sp-intro sp-faint">ChannelBin reads what the listing says. It cannot see what the channel actually shows.</p>
        ${data.groups.map((g, gi) => groupHtml(g, gi, opened.has(gi))).join('')}
        ${namesHtml(data.names, data.one)}${foot}`;
    };
    const load = () => jsonFetch(`/api/sports/events/${eventId}/listings`).then((d) => { data = d; draw(); })
      .catch((err) => { body.innerHTML = `<p class="sp-intro">${escHtml(err.message)}</p>`; });
    body.addEventListener('click', (e) => {
      const toggle = e.target.closest('[data-toggle]');
      if (toggle) {
        const gi = Number(toggle.dataset.toggle);
        if (opened.has(gi)) opened.delete(gi); else opened.add(gi);
        draw();
        return;
      }
      const btn = e.target.closest('[data-wrong]');
      if (!btn) return;
      const ids = btn.dataset.group !== undefined
        ? data.groups[Number(btn.dataset.group)].channels.map((c) => c.id)
        : [Number(btn.dataset.match)];
      const wrong = btn.dataset.wrong === '1';
      btn.disabled = true;
      jsonFetch(`/api/sports/events/${eventId}/wrong`, { method: 'POST', body: JSON.stringify({ match_ids: ids, wrong }) })
        .then(() => { changed = true; return load(); })
        .catch((err) => { btn.disabled = false; showToast(err.message, { type: 'error' }); });
    });
    load();
  }

  /* ---- Names for this league, and its categories ------------------------------------------- */
  const termsNow = () => JSON.parse((document.getElementById('sp-matching') || {}).dataset.terms || '[]');

  function saveTerms(terms) {
    return jsonFetch(`/api/sports/leagues/${leagueId()}/terms`, { method: 'PUT', body: JSON.stringify({ terms }) })
      .then(() => { showToast('Saved. ChannelBin checks your guide again in a minute.'); return refresh(); })
      .catch((err) => showToast(err.message, { type: 'error' }));
  }

  function saveCategories(change) {
    return jsonFetch(`/api/sports/leagues/${leagueId()}/categories`, { method: 'PUT', body: JSON.stringify(change) })
      .then(refresh)
      .catch((err) => { showToast(err.message, { type: 'error' }); return refresh(); });
  }

  function openTie() {
    const modal = buildModal({
      title: 'Add a category',
      body: '<div class="sp-dialog-body"><p class="sp-intro">Loading&hellip;</p></div>',
      footer: [{ label: 'Cancel', class: 'btn' }, {
        label: 'Add', class: 'btn btn-primary',
        onClick: (close) => {
          const names = Array.from(modal.querySelectorAll('.sp-offer input:checked')).map((i) => i.value);
          if (!names.length) { showToast('Tick at least one category.', { type: 'error' }); return false; }
          close();
          saveCategories({ add: names });
          return false;
        },
      }],
    });
    const body = modal.querySelector('.sp-dialog-body');
    jsonFetch(`/api/sports/leagues/${leagueId()}/categories/offer`).then((d) => {
      const offer = d.categories.filter((c) => !c.tied);
      body.innerHTML = `<p class="sp-intro">Categories on your accounts whose name has one of this league's names in it. A listing on a channel in a category you add counts as this league even when its title does not name it. A game still has to name both teams.</p>
        ${offer.length ? `<div class="sp-offer">${offer.map((c) => `<label class="sp-offer-row"><input type="checkbox" value="${escHtml(c.name)}"> <span>${escHtml(c.name)}</span><span class="sp-cat">${c.channels} ${c.channels === 1 ? 'channel' : 'channels'}</span></label>`).join('')}</div>`
    : '<p class="sp-intro">None of your categories has one of these names in it. Add a name above first if your provider spells the league differently.</p>'}`;
    }).catch((err) => { body.innerHTML = `<p class="sp-intro">${escHtml(err.message)}</p>`; });
  }

  document.addEventListener('submit', (e) => {
    const form = e.target.closest('form[data-act="add-term"]');
    if (!form) return;
    e.preventDefault();
    const value = form.querySelector('[name="term"]').value.trim();
    if (!value) return;
    saveTerms([...termsNow(), value]);
  });

  document.addEventListener('change', (e) => {
    const sw = e.target.closest('input[data-act="replay"]');
    if (sw) saveCategories({ replay: { [sw.dataset.name]: sw.checked } });
  });

  /* ---- Wiring ---------------------------------------------------------------------------- */
  const sectionLayout = PAGE ? initSectionLayout({
    config: { sections: PAGE.sections, sectionPref: PAGE.sectionPref },
    saveUrl: `/api/user-prefs/${PAGE.sectionsPrefKey}`,
    names: PAGE.sectionNames,
    note: 'The title, status bar and warning banners always show. The same layout applies to every league.',
  }) : null;

  bindNavClicks(document, (e) => {
    const row = e.target.closest('.sp-list .row[data-href]');
    return row && !e.target.closest('.c-actions, .menu, a, [data-tip]') ? row.dataset.href : null;
  });

  document.addEventListener('click', (e) => {
    if (e.target.closest('#sp-add')) { openAdd(); return; }
    const cell = e.target.closest('#sp-head [data-sort]');
    if (cell) { sortBy(cell.dataset.sort); return; }
    const field = e.target.closest('[data-sortfield]');
    if (field) { closeMenus(); sortBy(field.dataset.sortfield); return; }
    const act = e.target.closest('[data-act]');
    if (!act) return;
    closeMenus();
    const name = act.dataset.act;
    if (name === 'sections') { if (sectionLayout) sectionLayout.open(); return; }
    if (name === 'listings') { e.preventDefault(); openListings(Number(act.dataset.event), act.dataset.name, act.dataset.namesOnly === '1'); return; }
    if (name === 'remove-term') {
      const left = termsNow().filter((t) => t !== act.dataset.term);
      if (!left.length) { showToast('A league needs at least one name, or no listing could ever match it.', { type: 'error' }); return; }
      saveTerms(left);
      return;
    }
    if (name === 'accept-term') { saveTerms([...termsNow(), act.dataset.term]); return; }
    if (name === 'tie') { openTie(); return; }
    if (name === 'untie') { saveCategories({ remove: [act.dataset.name] }); return; }
    if (name === 'add-term' || name === 'replay') return;
    const d = dialogOf(act);
    if (!d) return;
    if (name === 'teams') openTeams(d);
    else if (name === 'stop') stop(d);
    else if (name === 'refollow') refollow(d);
    else if (name === 'delete') del(d);
  });

  applySort();
})();
