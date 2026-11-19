/* The Sports Schedules pages (templates/sports.html, sports_league.html, mockup 50,
   dev/docs/DESIGN-sports-data.md 5 and 11).

   Add league or team, Change teams, Stop following and Delete league, the listings dialog's Wrong match,
   and a league's names and categories (mockup 51), then a fresh server render
   (swapFromServer), so the templates stay the one renderer. A row and the league's action bar carry
   what the dialogs need as data attributes, which come back with that render. Clicks are delegated,
   since a swap rebuilds the nodes. The list sorts on its own column headers (DESIGN.md 10.3).
   The same swap also runs on a timer, so the page keeps up while it is open (see liveTick). */
(() => {
  const BOOT = readBootJson('sports-boot', { shipped: [], site: '' });
  const PAGE = window.SPORTS_LEAGUE || null;

  const siteLink = (text = 'TheSportsDB', path = '') =>
    `<a href="${escHtml(BOOT.site + path)}" target="_blank" rel="noopener">${escHtml(text)}</a>`;

  const REGIONS = PAGE
    ? ['.gd-head', '.gd-statezone', '#sp-sections']
    : ['.page-head', '#sports-body'];

  // Swaps run one after another. A timed swap fetched before an action's must never land after
  // it and put back what the action just changed.
  let swapQueue = Promise.resolve();
  let swapsPending = 0;
  const swap = (regions = REGIONS) => {
    swapsPending += 1;
    // A swap redraws the rows, so what the page holds about them is put back on (the sort, a new category's mark).
    const run = swapQueue.then(() => swapFromServer(regions)).then(applySort).then(markNewCategories);
    swapQueue = run.catch(() => {}).finally(() => { swapsPending -= 1; });
    return run;
  };
  const refresh = () => swap().catch(() => window.location.reload());

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

  /* ---- Add league or team ------------------------------------------------------------------
     Three steps on one modal: the entry (Cancel, Search), the "which one" list (Back, Cancel) and
     the confirm (Back, Cancel, Follow). Back returns to the entry with the typed text kept. Each
     step rebuilds the body, so its listeners go on the elements it draws, never on the body. */
  const isShippedName = (text) => BOOT.shipped.some((l) => l.name.toLowerCase() === text.trim().toLowerCase());

  function listHtml(needle) {
    const q = needle.trim().toLowerCase();
    const rows = BOOT.shipped.filter((l) => !q || l.name.toLowerCase().includes(q) || l.sport.toLowerCase().includes(q));
    const picks = rows.map((l) => `<button type="button" class="sp-pick" data-pick="${l.tsdb_id}"${l.followed ? ' disabled' : ''}>
        <span>${escHtml(l.name)}</span><span class="meta">${l.followed ? 'Following' : escHtml(l.sport)}</span></button>`).join('');
    const hint = !q
      ? `<span class="form-hint sp-lookup">Not listed? Type a team name, a league's number, or paste a link to a league's or team's page on ${siteLink()}.</span>`
      : rows.length ? ''
        : '<span class="form-hint sp-lookup">Search finds a team and its league, or checks a league\'s number or a link to a league\'s or team\'s page.</span>';
    return `<div class="sp-pick-list">${picks}</div>${hint}`;
  }

  // Only a paid key or a changed API returns more than one row, so only then can some be defunct and the rest not.
  const skippedHtml = (answer) => ((answer.skipped || []).length
    ? `<div class="sp-msg">Left out, as defunct: ${escHtml(answer.skipped.join(', '))}.</div>` : '');

  function openAdd() {
    const body = document.createElement('div');
    const modal = buildModal({ title: 'Add league or team', body, footer: [{ label: 'Cancel' }] });
    const foot = modal.querySelector('.modal-foot');
    let typed = '';
    let busy = false;
    let onEntry = true;
    let posting = false;
    // Escape on a later step goes back to the entry, as on the other stepped panels (dev/changelog/1161).
    modal.escapeHandler = () => {
      if (posting) return;
      if (onEntry) modal.closeModal(); else showEntry();
    };

    // duplicated from static/js/util.js buildModal's footer buttons - those are built once, and this
    // dialog swaps its footer per step on the one modal.
    const button = (label, cls, onClick, iconName = null) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = cls;
      b.innerHTML = `${iconName ? icon(iconName) : ''}${escHtml(label)}`;
      b.addEventListener('click', onClick);
      return b;
    };
    const setFoot = (...buttons) => { foot.innerHTML = ''; foot.append(...buttons); };
    const cancelBtn = () => button('Cancel', 'btn', () => modal.closeModal());
    const backBtn = () => button('Back', 'btn', () => showEntry(), 'back');
    const setMsg = (text, cls = 'bad') => {
      const msg = body.querySelector('#sp-msg');
      if (!msg) return;
      msg.innerHTML = text ? `<div class="sp-msg ${cls}">${escHtml(text).replace('its page on TheSportsDB', `its page on ${siteLink()}`)}</div>` : '';
    };

    function showEntry() {
      onEntry = true;
      body.innerHTML = `<div class="form-group sp-entry-group">
          <label for="sp-entry">League, team or link</label>
          <input id="sp-entry" class="form-control" type="text" autocomplete="off" placeholder="NFL, Boston Bruins, or a link">
        </div>
        <div id="sp-results"></div><div id="sp-msg"></div>`;
      const input = body.querySelector('#sp-entry');
      const results = body.querySelector('#sp-results');
      input.value = typed;
      const search = button('Search', 'btn btn-primary', () => {
        search.disabled = true;
        lookup({ text: input.value }).finally(sync);
      });
      function sync() {
        search.disabled = busy || !input.value.trim() || isShippedName(input.value);
      }
      input.addEventListener('input', () => {
        typed = input.value;
        setMsg('');
        results.innerHTML = listHtml(typed);
        sync();
      });
      input.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        if (!search.disabled) search.click();
      });
      results.addEventListener('click', (e) => {
        const pick = e.target.closest('[data-pick]');
        if (pick && !pick.disabled) lookup({ tsdb_id: Number(pick.dataset.pick) });
      });
      results.innerHTML = listHtml(typed);
      setFoot(cancelBtn(), search);
      sync();
      setTimeout(() => input.focus(), 0);
    }

    function showChoices(answer) {
      onEntry = false;
      body.innerHTML = `<div class="sp-msg">Which one do you mean?</div>
        <div class="sp-pick-list">${answer.choices.map((c, i) => `<button type="button" class="sp-pick" data-choice="${i}"><span>${escHtml(c.team)}</span><span class="meta">${escHtml(c.league.name)}</span></button>`).join('')}</div>
        ${skippedHtml(answer)}`;
      body.querySelector('.sp-pick-list').addEventListener('click', (e) => {
        const b = e.target.closest('[data-choice]');
        if (!b) return;
        const c = answer.choices[Number(b.dataset.choice)];
        showConfirm(c.league, c, {});
      });
      setFoot(backBtn(), cancelBtn());
    }

    function showConfirm(league, team, answer) {
      const leaguePage = `league/${league.tsdb_id}`;
      body.innerHTML = `<div class="sp-confirm-name">${escHtml(league.name)}</div>
        <div class="sp-confirm-sub">${[league.sport, league.season ? `${league.season} season` : null].filter(Boolean).map(escHtml).join(', ')}${league.sport || league.season ? ', ' : ''}${siteLink(`TheSportsDB league ${league.tsdb_id}`, leaguePage)}</div>
        ${team ? `<div class="form-group sp-follow-group">
          <label class="sp-radio"><input type="radio" name="sp-follow" value="team" checked> Only ${escHtml(team.team)}</label>
          <label class="sp-radio"><input type="radio" name="sp-follow" value="all"> All games in the league</label>
          <span class="form-hint">ChannelBin gets the whole league's schedule either way, so more teams can be picked once it is here.</span>
        </div>` : ''}
        ${answer.best_match ? '<div class="sp-msg" id="sp-best">TheSportsDB gives only its best match for a name. If this is not the team you meant, go back and type more of it, such as the city and nickname.</div>' : ''}
        ${skippedHtml(answer)}
        <div class="sp-msg">Getting a new schedule can take up to an hour, because the calls are spread out. After that it costs a few calls a day.</div>
        <div id="sp-msg-2"></div>`;
      onEntry = false;
      const back = backBtn();
      const msg2 = body.querySelector('#sp-msg-2');
      // While a follow is posting, Back (and Escape) must not swap the step out from under its answer.
      const go = button('Follow', 'btn btn-primary', async () => {
        const choice = body.querySelector('input[name="sp-follow"]:checked');
        const teamId = team && choice && choice.value === 'team' ? team.team_id : null;
        go.disabled = true;
        back.disabled = true;
        posting = true;
        try {
          await jsonFetch('/api/sports/leagues', {
            method: 'POST', body: JSON.stringify({ tsdb_id: league.tsdb_id, team_id: teamId }),
          });
          modal.closeModal();
          showToastAfterLoad(`Following ${league.name}. Getting its schedule.`);
          window.location.reload();
        } catch (err) {
          go.disabled = false;
          back.disabled = false;
          msg2.innerHTML = `<div class="sp-msg bad">${escHtml(err.message)}</div>`;
        } finally {
          posting = false;
        }
      });
      setFoot(back, cancelBtn(), go);
    }

    // One lookup at a time: a second straight after the first would only be told to wait.
    async function lookup(payload) {
      if (busy) return;
      busy = true;
      setMsg('');
      let answer;
      try {
        answer = await jsonFetch('/api/sports/lookup', { method: 'POST', body: JSON.stringify(payload) });
      } catch (err) {
        setMsg(err.message, err.status === 409 ? 'warn' : 'bad');
        return;
      } finally {
        busy = false;
      }
      if (answer.league) showConfirm(answer.league, answer.team || null, answer);
      else showChoices(answer);
    }

    showEntry();
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
          }).then((data) => { saidCut(data); close(); return refresh(); })
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
  // d.one and d.many are the league's own noun (sports_view._dialog), so a race is never a game.
  const gamesWord = (d) => `${d.stored} upcoming ${d.stored === 1 ? d.one : d.many}`;

  // A person's act that stopped a sports channel being served ended the plays of it
  // (output_feed.stop_unserved_sports); say so, since the IPTV player just went dark.
  const saidCut = (data) => {
    if (data && data.cut) {
      showToast(`${data.cut === 1 ? 'An IPTV player' : `${data.cut} IPTV players`} watching a sports channel `
        + `that is no longer on the IPTV output ${data.cut === 1 ? 'was' : 'were'} cut.`, { type: 'warning' });
    }
    return data;
  };

  async function stop(d) {
    const ok = await confirmModal({
      title: `Stop following ${d.name}?`,
      message: `ChannelBin stops checking TheSportsDB for ${d.name} ${d.many}.`,
      consequence: `${d.stored ? `It keeps the ${gamesWord(d)} it already has, and deletes each one ${d.keep_days} days after it's over. ` : ''}The league stays in this list so you can follow it again.`,
      confirmLabel: 'Stop following',
    });
    if (!ok) return;
    jsonFetch(`/api/sports/leagues/${d.id}/following`, { method: 'DELETE' }).then(saidCut).then(refresh)
      .catch((err) => showToast(err.message, { type: 'error' }));
  }

  function refollow(d) {
    jsonFetch(`/api/sports/leagues/${d.id}/following`, { method: 'PUT', body: JSON.stringify({ team_ids: [] }) })
      .then(() => { showToast(`Following ${d.name} again.`); return refresh(); })
      .catch((err) => showToast(err.message, { type: 'error' }));
  }

  async function del(d) {
    // A league an auto-record rule names cannot be removed (app/sports_data.py::delete_league), so
    // the page says so before anything is pressed rather than after (dev/changelog/1491).
    if (d.rules && d.rules.length) {
      const names = d.rules.map((n) => `"${n}"`).join(', ');
      showToast(`${d.name} cannot be deleted: ${d.rules.length === 1 ? 'the auto-record rule' : 'the auto-record rules'} `
        + `${names} ${d.rules.length === 1 ? 'names' : 'name'} it. Change ${d.rules.length === 1 ? 'it' : 'them'} `
        + 'to name something else first.', { type: 'error', durationMs: 10000 });
      return;
    }
    const ok = await confirmModal({
      title: `Delete ${d.name}?`,
      message: `This deletes ${d.name} from ChannelBin, with its stored ${d.many} and its team names.`,
      consequence: 'You can add the league again later, and its schedule will be fetched again.',
      confirmLabel: 'Delete league', danger: true,
    });
    if (!ok) return;
    jsonFetch(`/api/sports/leagues/${d.id}`, { method: 'DELETE' }).then((data) => {
      saidCut(data);
      const leave = () => { window.location.href = PAGE.listUrl; }; // nav-ok: the page this was on no longer exists
      // A cut play is said before the page goes, since the toast would go with it.
      if (PAGE) { if (data && data.cut) setTimeout(leave, 2500); else leave(); }
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
      <p class="sp-intro">These channels have no real listing in your guide at this ${one}'s time: nothing at all then, or only their own name repeated. Each one's name says it carries this ${one}, and the name is all ChannelBin has to go on. Providers rename these channels as the day goes on, so the names are read again after every sync, and a channel whose name moves on to another ${one} drops off this list.</p>
      <div class="sp-enames">${rows}</div>`;
  }

  /* The listings a word or a length kept out (app/sports_match.py::skipped_listings, mockup 59,
     dev/changelog/1634), behind Show, which turns into Hide while they are open. */
  function skippedListingsHtml(data, open) {
    const rows = data.skipped || [];
    if (!rows.length) return '';
    const n = rows.length;
    const toggle = open
      ? '<button class="btn btn-sm sp-skip-show" data-skipped="hide" aria-expanded="true">Hide skipped listings</button>'
      : `<button class="btn btn-sm sp-skip-show" data-skipped="show" aria-expanded="false">Show ${n} skipped ${n === 1 ? 'listing' : 'listings'}</button>`;
    const list = open ? rows.map((r) => `<div class="sp-group sp-skipped">
        <div class="sp-ghead"><span class="sp-gtitle">${escHtml(r.sub ? `${r.title}: ${r.sub}` : r.title)}</span><span class="sp-gmeta">${escHtml(r.when)}</span></div>
        <p class="sp-why"><strong>${r.channels} ${r.channels === 1 ? 'channel' : 'channels'}</strong>. ${escHtml(r.why)}</p></div>`).join('') : '';
    return `<h3 class="sp-h3">Skipped <span class="cnt">${n}</span></h3>
      <p class="sp-intro">These listings name ${escHtml(data.league)}${data.matchup ? ' and both teams' : ` on this ${escHtml(data.one)}'s day`}, but ChannelBin does not count them as this ${escHtml(data.one)}, because of a word in them or their length. The words and the lengths are on the league page, under Matching your guide.</p>
      ${toggle}${list}`;
  }

  function openListings(eventId, name, namesOnly) {
    let data = null;
    let changed = false;
    let skippedOpen = false;
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
            <p class="sp-intro sp-faint">If a name is wrong for this ${data.one}, press its <strong>Wrong match</strong>. Nothing is recorded, cancelled or deleted, and Undo puts it back. ChannelBin reads what the name says. It cannot see what the channel actually shows.</p>${skippedListingsHtml(data, skippedOpen)}${foot}`
          : `<p class="sp-intro">Nothing in your guide matches it now.</p>${skippedListingsHtml(data, skippedOpen)}${foot}`;
        return;
      }
      body.innerHTML = `<p class="sp-intro">These are the <strong>${data.listings} ${data.listings === 1 ? 'listing' : 'listings'}</strong> on <strong>${data.channels} ${data.channels === 1 ? 'channel' : 'channels'}</strong> in your guide that ChannelBin believes are this one, grouped by how the listing is worded. Open a group to see its channels.</p>
        <p class="sp-intro">If ChannelBin got a group wrong, press its <strong>Wrong match</strong>. Those listings then stop counting: this count drops, ChannelBin works out the live airing and the guide's time without them, and anything that later uses this match, such as a recording's details or an auto-record rule, leaves them out. Nothing is recorded, cancelled or deleted, and Undo puts it back. Inside a group, each channel has its own Wrong match for the rare channel whose listing is wrong when the rest are right.</p>
        <p class="sp-intro sp-faint">ChannelBin reads what the listing says. It cannot see what the channel actually shows.</p>
        ${data.groups.map((g, gi) => groupHtml(g, gi, opened.has(gi))).join('')}
        ${namesHtml(data.names, data.one)}${skippedListingsHtml(data, skippedOpen)}${foot}`;
    };
    const load = () => jsonFetch(`/api/sports/events/${eventId}/listings`).then((d) => { data = d; draw(); })
      .catch((err) => { body.innerHTML = `<p class="sp-intro">${escHtml(err.message)}</p>`; });
    body.addEventListener('click', (e) => {
      if (e.target.closest('[data-skipped]')) { skippedOpen = !skippedOpen; draw(); return; }
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

  /* A team's added names (app/sports_match.py::set_team_names). The whole list for one team is
     sent each time, read from the page as the server last drew it. */
  const teamsNow = () => JSON.parse((document.getElementById('sp-matching') || {}).dataset.teams || '[]');

  function saveTeamNames(teamId, names) {
    return jsonFetch(`/api/sports/teams/${teamId}/names`, { method: 'PUT', body: JSON.stringify({ names }) })
      .then(() => { showToast('Saved. ChannelBin checks your guide again in a minute.'); return refresh(); })
      .catch((err) => showToast(err.message, { type: 'error' }));
  }

  function openTeamName() {
    const teams = teamsNow();
    const modal = buildModal({
      title: 'Add a name for a team',
      body: `<div class="sp-dialog-body">
        <p class="sp-intro">Type the team's name the way your guide or your provider's channel names spell it. It is read exactly like the names from TheSportsDB, and it stays when those are read again.</p>
        <div class="form-group"><label for="sp-tn-team">Team</label>
          <select class="form-control" id="sp-tn-team"><option value="">Pick a team</option>${teams.map((t) => `<option value="${t.id}">${escHtml(t.name)}</option>`).join('')}</select></div>
        <p class="sp-intro sp-faint" id="sp-tn-theirs" hidden></p>
        <div class="form-group"><label for="sp-tn-name">Name</label>
          <input class="form-control" id="sp-tn-name" maxlength="120" autocomplete="off"></div>
      </div>`,
      footer: [{ label: 'Cancel', class: 'btn' }, {
        label: 'Add', class: 'btn btn-primary',
        onClick: (close) => {
          const team = teams.find((t) => String(t.id) === modal.querySelector('#sp-tn-team').value);
          const value = modal.querySelector('#sp-tn-name').value.trim();
          if (!team) { showToast('Pick a team.', { type: 'error' }); return false; }
          if (!value) { showToast('Type a name.', { type: 'error' }); return false; }
          close();
          saveTeamNames(team.id, [...team.own, value]);
          return false;
        },
      }],
    });
    const theirs = modal.querySelector('#sp-tn-theirs');
    modal.querySelector('#sp-tn-team').addEventListener('change', (e) => {
      const team = teams.find((t) => String(t.id) === e.target.value);
      theirs.hidden = !team;
      if (team) {
        const have = [...team.theirs, ...team.own].map((n) => `"${n}"`).join(', ');
        theirs.textContent = `Read as ${have} now.`;
      }
    });
  }

  /* A category just added lands in name order, so every row under it moves down one, and one whose
     name says replay arrives with Replays only already on. A tap aimed at where a row used to be
     then lands on its neighbor (dev/changelog/1582). So the new rows are marked, the toast says
     what was switched on, and a Replays only tap is put back and asked for again from the add's
     answer until SETTLE_MS after the moved rows are drawn. The mark comes off after NEW_MS. */
  const SETTLE_MS = 2500;
  const NEW_MS = 60 * 1000;
  let settlingUntil = 0;
  let newCategories = [];
  let rowsMoving = false;

  function markNewCategories() {
    if (rowsMoving) { rowsMoving = false; settlingUntil = Date.now() + SETTLE_MS; }
    document.querySelectorAll('#sp-matching tr[data-cat]').forEach((row) => {
      const fresh = newCategories.includes(row.dataset.cat);
      row.classList.toggle('sp-cat-new', fresh);
      if (!fresh) row.querySelectorAll('.sp-cat-newbadge').forEach((b) => b.remove());
      if (fresh && !row.querySelector('.sp-cat-newbadge')) {
        row.querySelector('.t').insertAdjacentHTML('beforeend', ' <span class="badge b-sched sp-cat-newbadge">New</span>');
      }
    });
  }

  function saveCategories(change) {
    return jsonFetch(`/api/sports/leagues/${leagueId()}/categories`, { method: 'PUT', body: JSON.stringify(change) })
      .then((d) => {
        const added = d.added || [];
        if (added.length) {
          newCategories = added;
          rowsMoving = true;
          settlingUntil = Infinity;
          setTimeout(() => { if (newCategories === added) { newCategories = []; markNewCategories(); } }, NEW_MS);
          const on = d.replay_on || [];
          const what = added.length === 1 ? `Added "${added[0]}".` : `Added ${added.length} categories.`;
          const auto = on.length
            ? ` Replays only is on for ${on.map((n) => `"${n}"`).join(' and ')}, because the name says replay. Turn it off if it shows live games.`
            : '';
          showToast(`${what}${auto} The rows below ${added.length === 1 ? 'it' : 'them'} moved down.`, { type: on.length ? 'warning' : 'success', durationMs: 9000 });
        }
        return refresh();
      })
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

  /* ---- The words and lengths that keep a listing out (mockup 59, dev/changelog/1634) ---------
     Each change saves at once; the swap then draws the list as the server holds it. */
  const skipsUrl = (what) => `/api/sports/leagues/${leagueId()}/${what}`;
  const skipSaved = () => { showToast('Saved. ChannelBin checks your guide again in a minute.', { type: 'success' }); return refresh(); };
  const skipFailed = (err) => { showToast(err.message, { type: 'error' }); return refresh(); };

  // The list the page shows: the server writes the change to that one.
  const skipKind = () => (document.getElementById('sp-skips') || {}).dataset.skipKind;
  function saveSkipWord(body) {
    return jsonFetch(skipsUrl('skip-words'), { method: 'PUT', body: JSON.stringify({ ...body, kind: skipKind() }) })
      .then(skipSaved).catch(skipFailed);
  }

  function restoreSkips() {
    confirmModal({
      title: 'Restore defaults',
      message: "Restore ChannelBin's default words and lengths for this league? Every word you added is taken off, every one you removed is put back, and the lengths go back to the defaults.",
      consequence: 'Listings may count again, or stop counting, at the next check of your guide.',
      confirmLabel: 'Restore defaults',
    }).then((ok) => {
      if (!ok) return;
      jsonFetch(skipsUrl('skip-words'), { method: 'DELETE' })
        .then(() => { showToast('Restored. ChannelBin checks your guide again in a minute.', { type: 'success' }); return refresh(); })
        .catch(skipFailed);
    });
  }

  document.addEventListener('submit', (e) => {
    const skip = e.target.closest('form[data-act="add-skip"]');
    if (skip) {
      e.preventDefault();
      const value = skip.querySelector('[name="word"]').value.trim();
      if (value) saveSkipWord({ add: value });
      return;
    }
    const form = e.target.closest('form[data-act="add-term"]');
    if (!form) return;
    e.preventDefault();
    const value = form.querySelector('[name="term"]').value.trim();
    if (!value) return;
    saveTerms([...termsNow(), value]);
  });

  /* A sports channel on the output feed: a team's (data-team is its id) or the league's numbered
     ones (0). The page is swapped from the server either way, so a refused switch goes back. */
  function saveFeedChannel(teamId, enabled) {
    return jsonFetch(`/api/sports/leagues/${leagueId()}/feed-channels`,
      { method: 'PUT', body: JSON.stringify({ team_id: teamId, enabled }) })
      .then(saidCut).then(refresh)
      .catch((err) => { showToast(err.message, { type: 'error' }); return refresh(); });
  }

  /* The league's Up next channel on the output feed (app/sports_up_next.py). */
  function saveUpNext(enabled) {
    return jsonFetch('/api/sports/up-next', { method: 'PUT', body: JSON.stringify({ league_id: leagueId(), enabled }) })
      .then(saidCut).then(refresh)
      .catch((err) => { showToast(err.message, { type: 'error' }); return refresh(); });
  }

  /* ---- Game time settings (mockup 58, dev/changelog/1630) ---------------------------------
     Each control saves on change, like the IPTV Output page's settings; the swap then draws the
     rows the answer shows. On a league page an empty choice or box is the default again. */
  function saveGameTime(field) {
    const card = field.closest('[data-gt-url]');
    const value = field.type === 'number' && field.value !== '' ? Number(field.value) : field.value;
    jsonFetch(card.dataset.gtUrl, { method: 'PUT', body: JSON.stringify({ [field.dataset.gtField]: value }) })
      .then(() => { showToast('Saved.', { type: 'success' }); return refresh(); })
      .catch((err) => { showToast(err.message, { type: 'error' }); refresh(); });
  }

  document.addEventListener('change', (e) => {
    const gt = e.target.closest('[data-gt-url] [data-gt-field]');
    if (gt) { saveGameTime(gt); return; }
    const len = e.target.closest('input[data-skip-length]');
    if (len) {
      jsonFetch(skipsUrl('skip-minutes'), { method: 'PUT', body: JSON.stringify({ length: len.dataset.skipLength, minutes: len.value === '' ? null : Number(len.value) }) })
        .then(skipSaved).catch(skipFailed);
      return;
    }
    const sw = e.target.closest('input[data-act="replay"]');
    if (sw) {
      if (Date.now() < settlingUntil) {
        sw.checked = !sw.checked;
        showToast('The list just changed, so that tap was not saved. Check the row and tap again.', { type: 'warning' });
        return;
      }
      saveCategories({ replay: { [sw.dataset.name]: sw.checked } });
      return;
    }
    const feed = e.target.closest('input[data-act="feed-channel"]');
    if (feed) saveFeedChannel(Number(feed.dataset.team), feed.checked);
    const upNext = e.target.closest('input[data-act="up-next"]');
    if (upNext) saveUpNext(upNext.checked);
  });

  /* ---- A page at a time (mockup 57) --------------------------------------------------------
     Upcoming games and On your IPTV output are paged on the server (?page=, ?feed_page=), and the
     channels filtered by team first (?feed_q=). Prev and Next are links, so a new tab opens that
     page. A plain click, or the filter, puts the address in the bar and swaps only that card's
     list through the one queue: the timed swap fetches the address, so it keeps the page. */
  const PAGED = { games: '#sp-upcoming', feed: '#sp-feed-list' };
  const PAGE_PARAM = { games: 'page', feed: 'feed_page' };
  const FILTER_MS = 250;
  let filterTimer = null;

  // After a page change, bring the card's heading back when it has scrolled off the top, below the
  // phone's top bar.
  function toCardTop(sel) {
    const region = document.querySelector(sel);
    const head = region && region.closest('.card').querySelector('.card-head');
    if (!head) return;
    const bar = isPhone() ? parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--topnav-h')) || 0 : 0;
    const top = head.getBoundingClientRect().top;
    if (top < bar) window.scrollTo({ top: window.scrollY + top - bar - 8 });
  }

  document.addEventListener('click', (e) => {
    const link = e.target.closest('a[data-pager]');
    if (!link || wantsNewTab(e)) return;
    e.preventDefault();
    const kind = link.dataset.pager;
    const sel = PAGED[kind];
    // Only this card's page moves. The link was drawn with the other card as it was then, and
    // that card may have paged or filtered since without this one being redrawn.
    const url = new URL(window.location.href);
    const to = new URL(link.href).searchParams.get(PAGE_PARAM[kind]);
    if (to) url.searchParams.set(PAGE_PARAM[kind], to); else url.searchParams.delete(PAGE_PARAM[kind]);
    window.history.replaceState(null, '', url);
    swap([sel]).then(() => toCardTop(sel)).catch(() => window.location.reload());
  });

  function filterFeed() {
    clearTimeout(filterTimer);
    const input = document.getElementById('sp-feed-q');
    if (!input) return;
    const url = new URL(window.location.href);
    const q = input.value.trim();
    if (q) url.searchParams.set('feed_q', q); else url.searchParams.delete('feed_q');
    url.searchParams.delete('feed_page');
    window.history.replaceState(null, '', url);
    // The field now says what the address says, so it no longer holds the timed swap off (inUse).
    input.defaultValue = input.value;
    swap([PAGED.feed]).catch(() => window.location.reload());
  }

  const markFilter = (input) => {
    const wrap = document.getElementById('sp-feed-wrap');
    if (wrap) wrap.classList.toggle('has-text', input.value.length > 0);
  };

  document.addEventListener('input', (e) => {
    if (e.target.id !== 'sp-feed-q') return;
    markFilter(e.target);
    clearTimeout(filterTimer);
    filterTimer = setTimeout(filterFeed, FILTER_MS);
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('#sp-feed-clear')) return;
    const input = document.getElementById('sp-feed-q');
    input.value = '';
    markFilter(input);
    filterFeed();
    input.focus();
  });

  /* ---- Record, and a game's recording (mockup 54, dev/changelog/1539) ----------------------
     Record asks the server what to record: it works the pick out from the game alone, the way an
     auto-record rule for the league would, and the shared record dialog (guide.js::openModal)
     opens on it with one notice saying which listing and why. A greyed Record keeps its click to
     say why. Manage recording's entries are the Recordings list's, with its confirm words; every
     change ends in the page's own swap from the server. */
  window.onScheduleSaved = () => { refresh(); };

  async function record(btn) {
    if (btn.dataset.why) { showToast(btn.dataset.why, { type: 'warning' }); return; }
    if (btn.dataset.busy) return;
    btn.dataset.busy = '1';
    try {
      const pick = await jsonFetch(`/api/sports/events/${btn.dataset.event}/record`);
      const notice = { ...pick.notice, searchUrl: pick.search_url || '' };
      if (pick.kind === 'event') {
        openModal(pick.program, pick.channel, { notice, sportsEventId: pick.sports_event_id });
        return;
      }
      const group = pick.group_id ? `?group=${pick.group_id}` : '';
      const ctx = await jsonFetch(`/api/channels/airings/${pick.epg_id}/record-context${group}`);
      // pick.times: where the league's answer to which time a game goes by puts the listing
      // (sports_view.listing_times()); the profile's padding goes on top, as for any listing.
      openModal(ctx.program, ctx.channel, { notice, sportsEventId: pick.sports_event_id, times: pick.times });
    } catch (err) {
      // A 409 is the page being out of date: a recording made elsewhere, or nothing left.
      showToast(err.message, { type: 'error' });
      if (err.status === 409) refresh();
    } finally {
      delete btn.dataset.busy;
    }
  }

  function editRecording(btn) {
    const d = JSON.parse(btn.dataset.edit);
    const name = btn.dataset.recName;
    openModal({
      has_recording: true, recording_id: Number(btn.dataset.rec), recording_status: statusVocab('REC_STATUS').SCHEDULED,
      recording_start_time: d.start_iso, recording_stop_time: d.stop_iso, recording_profile_id: d.profile_id,
      recording_move_when_full: d.move_when_full,
      suggested_name: name, title: name, stream_url: d.url, channel_id: d.channel_id, group_id: d.group_id,
      id: null, description: null,
    }, null);
  }

  async function cancelRecording(btn) {
    const ok = await confirmModal({
      title: 'Cancel scheduled recording',
      message: `"${btn.dataset.recName}" will be removed.`,
      consequence: btn.dataset.consequence || '',
      confirmLabel: 'Cancel recording',
      danger: true,
    });
    if (!ok) return;
    jsonFetch(`/recordings/${btn.dataset.rec}/cancel-json`, { method: 'POST' })
      .catch((err) => showToast(err.message, { type: 'error' }))
      .finally(refresh);
  }

  async function stopRecording(btn) {
    const ok = await confirmModal({
      title: 'Stop recording',
      message: `Capture of "${btn.dataset.recName}" will end now and its segments will be joined into the final file.`,
      consequence: "It can't be resumed.",
      confirmLabel: 'Stop recording',
      danger: true,
    });
    if (!ok) return;
    jsonFetch(`/recordings/${btn.dataset.rec}/stop-json`, { method: 'POST' })
      .catch((err) => showToast(err.message, { type: 'error' }))
      .finally(refresh);
  }

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
    if (name === 'live-now') { goLiveNow(act).then((changed) => { if (changed) refresh(); }); return; }
    if (name === 'remove-term') {
      const left = termsNow().filter((t) => t !== act.dataset.term);
      if (!left.length) { showToast('A league needs at least one name, or no listing could ever match it.', { type: 'error' }); return; }
      saveTerms(left);
      return;
    }
    if (name === 'accept-term') { saveTerms([...termsNow(), act.dataset.term]); return; }
    if (name === 'team-name') { openTeamName(); return; }
    if (name === 'remove-team-name') {
      const team = teamsNow().find((t) => String(t.id) === act.dataset.team);
      if (team) saveTeamNames(team.id, team.own.filter((n) => n !== act.dataset.term));
      return;
    }
    if (name === 'tie') { openTie(); return; }
    if (name === 'untie') { saveCategories({ remove: [act.dataset.name] }); return; }
    if (name === 'remove-skip') { saveSkipWord({ remove: act.dataset.word }); return; }
    if (name === 'restore-skips') { restoreSkips(); return; }
    if (name === 'add-term' || name === 'add-skip' || name === 'replay' || name === 'feed-channel') return;
    if (name === 'record') { record(act); return; }
    if (name === 'search-why') { showToast(act.dataset.why, { type: 'warning' }); return; }
    if (name === 'edit-rec') { editRecording(act); return; }
    if (name === 'cancel-rec') { cancelRecording(act); return; }
    if (name === 'stop-rec') { stopRecording(act); return; }
    const d = dialogOf(act);
    if (!d) return;
    if (name === 'teams') openTeams(d);
    else if (name === 'stop') stop(d);
    else if (name === 'refollow') refollow(d);
    else if (name === 'delete') del(d);
  });

  /* ---- Keeping the page current ------------------------------------------------------------
     The worker calls TheSportsDB about once a minute while a schedule fills, and the guide match
     follows each refresh, so the status badge, the calls left, the games and Schedule updates
     all move while the page is open. Every LIVE_MS the regions an action swaps are swapped
     again from the server, so the templates stay the one renderer. A turn is skipped while the
     page is in use - a dialog or menu open, a text field holding something, text selected -
     because the swap would replace what the person is working on; the next turn catches up. A
     failed fetch (a restart, a dropped connection) skips a turn, where an action's reloads. */
  const LIVE_MS = 30 * 1000;
  const TEXT_FIELDS = 'input:not([type]), input[type="text"], input[type="search"], input[type="number"], textarea';

  const inUse = () => REGIONS.some((sel) => {
    const region = document.querySelector(sel);
    if (!region) return false;
    const field = document.activeElement;
    if (field && region.contains(field) && field.matches(TEXT_FIELDS)) return true;
    if (Array.from(region.querySelectorAll(TEXT_FIELDS)).some((f) => f.value !== f.defaultValue)) return true;
    return hasSelectionIn(region);
  });

  const liveTick = () => {
    if (document.hidden || swapsPending || overlayOpen() || inUse()) return;
    swap().catch((err) => console.warn('Sports page refresh failed; the next turn retries.', err));
  };
  setInterval(liveTick, LIVE_MS);
  // A hidden tab skips its turns, so it catches up the moment it is looked at again.
  document.addEventListener('visibilitychange', liveTick);

  applySort();
})();
