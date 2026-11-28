/* The IPTV Output page (templates/iptv_output.html, mockup 53, dev/changelog/1526).

   Every action posts, then the page's regions are swapped for the server's fresh render
   (swapFromServer), so the template stays the one renderer. What the swap would lose is held
   here and put back after it: the connection tab and the channel list's search, category,
   chip and page. Clicks are delegated, since a swap rebuilds the nodes. The live regions are swapped
   again on a timer so Watching now and Recent plays keep up while the page is open.

   No password is in the page. The eye and Copy ask the login's own gated read for it,
   /api/iptv-output/logins/<id>/password (dev/changelog/1760). Every login is a row, the owner's
   own included, and the Connect card shows the one picked at its top (dev/changelog/1781).

   Three sizes of swap. Turning the output on or off, the first login and the mode change which cards
   exist, so they take everything. A switch or a stopped play takes the cards but not Settings,
   and a saved number takes only the state bar, so a number being typed in another settings
   row is never replaced by the save of the one before it. */
(() => {
  const ALL = ['#of-state', '#of-sections'];
  const CARDS = ['#of-state', '#of-connect', '#of-logins', '#of-watching', '#of-pip', '#of-channels', '#of-recent', '#of-accounts'];
  const STATE_ONLY = ['#of-state'];
  // What moves on its own. The channel list and the settings rows are left out of the timer,
  // so a search in progress or a number being typed is never replaced under the person.
  const LIVE = ['#of-state', '#of-logins', '#of-watching', '#of-recent', '#of-accounts'];
  const LIVE_MS = 15 * 1000;
  const FIELD_URL = '/api/settings/field';

  // `login` is the Connect card's pick, '' for the first login.
  const view = { tab: 'xtream', login: '', q: '', cat: '', show: 'all', page: 1 };
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const players = (n) => `${n} IPTV player${n === 1 ? '' : 's'}`;
  const post = (url, body) => jsonFetch(url, { method: 'POST', body: JSON.stringify(body || {}) });
  const fail = (err) => showToast(err.message || 'That did not work.', { type: 'error' });

  /* ---- Held state, re-applied after every swap ------------------------------------------- */
  function applyTab() {
    $$('#of-connect [data-tab]').forEach((b) => {
      const on = b.dataset.tab === view.tab;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    $$('#of-connect [data-pane]').forEach((p) => { p.hidden = p.dataset.pane !== view.tab; });
  }

  function applyLogin() {
    const pick = $('#of-conn-login');
    const ids = $$('#of-connect [data-conn-login]').map((el) => el.dataset.connLogin);
    // A login deleted since it was picked: back to the first.
    if (!ids.includes(view.login)) view.login = ids[0] || '';
    if (pick) pick.value = view.login;
    $$('#of-connect [data-conn-login]').forEach((el) => { el.hidden = el.dataset.connLogin !== view.login; });
  }

  function rowMatches(tr) {
    const state = tr.dataset.state;
    if (view.show === 'on' && state !== 'on') return false;
    if (view.show === 'hidden' && state !== 'hidden') return false;
    if (view.show === 'problem' && tr.dataset.problem !== '1') return false;
    if (view.cat && tr.dataset.cat !== view.cat) return false;
    const q = view.q.trim().toLowerCase();
    return !q || tr.dataset.hay.includes(q);
  }

  function applyFilter() {
    const rows = $$('#of-rows tr[data-feed]');
    if (!rows.length) return;
    const cat = $('#of-cat');
    // A category whose last channel left the guide goes with it.
    if (view.cat && !Array.from(cat.options).some((o) => o.value === view.cat)) view.cat = '';
    cat.value = view.cat;
    const q = $('#of-q');
    if (q.value !== view.q) q.value = view.q;
    // The channels are paged over what the search and the filters leave, so a match on a
    // later page is never hidden behind a page of rows that did not match.
    const per = Number($('#of-rows').dataset.perPage) || rows.length;
    const matched = rows.filter(rowMatches);
    const pages = Math.max(1, Math.ceil(matched.length / per));
    view.page = Math.min(Math.max(1, view.page), pages);
    const first = (view.page - 1) * per;
    const onPage = new Set(matched.slice(first, first + per));
    rows.forEach((tr) => { tr.hidden = !onPage.has(tr); });
    $('#of-none').hidden = matched.length > 0;
    const pager = $('#of-chan-pager');
    pager.hidden = pages === 1;
    $('#of-chan-n').textContent = `${first + 1}-${first + onPage.size} of ${matched.length}`;
    $('[data-dir="-1"]', pager).disabled = view.page === 1;
    $('[data-dir="1"]', pager).disabled = view.page === pages;
    $$('#of-chips [data-show]').forEach((chip) => {
      const on = chip.dataset.show === view.show;
      chip.classList.toggle('active', on);
      chip.classList.toggle('tone-warn', on && chip.dataset.show === 'problem');
    });
  }

  const applyView = () => { applyTab(); applyLogin(); applyFilter(); };

  /* ---- Swapping ---------------------------------------------------------------------------
     One after another: a timed swap fetched before an action's must never land after it and
     put back what the action just changed. */
  let swapQueue = Promise.resolve();
  let swapsPending = 0;
  const swap = (regions) => {
    swapsPending += 1;
    const run = swapQueue.then(() => swapFromServer(regions)).then(applyView);
    swapQueue = run.catch(() => {}).finally(() => { swapsPending -= 1; });
    return run;
  };
  const refresh = (regions = ALL) => swap(regions).catch(() => window.location.reload());

  const inUse = () => LIVE.some((sel) => {
    const region = $(sel);
    if (!region) return false;
    const field = document.activeElement;
    return (field && region.contains(field) && field.matches('select')) || hasSelectionIn(region);
  });
  const liveTick = () => {
    if (document.hidden || swapsPending || overlayOpen() || inUse()) return;
    const before = $('#of-state').dataset.state;
    swap(LIVE)
      // Turned on or off from somewhere else: the cards themselves differ, so take them all.
      .then(() => ($('#of-state').dataset.state === before ? null : swap(ALL)))
      .catch((err) => console.warn('IPTV Output refresh failed; the next turn retries.', err));
  };

  /* ---- The password, on request ---------------------------------------------------------- */
  const passwordOf = (id) => jsonFetch(`/api/iptv-output/logins/${id}/password`).then((d) => d.password || '');
  // The Connect card's rows are inside the login they belong to.
  const fetchPassword = (el) => passwordOf(el.closest('[data-conn-login]').dataset.connLogin);
  const fill = (template, password) => template.split('{password}').join(encodeURIComponent(password));

  async function copyRow(btn) {
    const row = btn.closest('.of-conn');
    let text;
    if (btn.dataset.secret === undefined) {
      text = $('[data-copy-value]', row).textContent;
    } else {
      const password = await fetchPassword(btn);
      // The Xtream password is typed as it is; inside an address it is percent-encoded.
      text = btn.dataset.secret === '{password}' ? password : fill(btn.dataset.secret, password);
    }
    await copyText(text);
    showToast(`${btn.dataset.label} copied.`);
  }

  async function revealAddress(btn) {
    const value = $('[data-secret-text]', btn.closest('.of-conn'));
    const shown = btn.dataset.shown === '1';
    const template = value.dataset.secretText;
    // What the server drew, kept so hiding puts back exactly that.
    if (value.dataset.masked === undefined) value.dataset.masked = value.textContent;
    if (shown) value.textContent = value.dataset.masked;
    // The Xtream password is shown as it is typed; inside an address it is percent-encoded.
    else if (template === '{password}') value.textContent = await fetchPassword(btn);
    else value.textContent = fill(template, await fetchPassword(btn));
    btn.dataset.shown = shown ? '' : '1';
    btn.innerHTML = icon(shown ? 'reveal' : 'conceal');
    btn.setAttribute('aria-label', `${shown ? 'Show' : 'Hide'} the password`);
  }

  /* ---- Actions ---------------------------------------------------------------------------- */
  async function setPower(to) {
    if (to === 'off') {
      const n = Number($('#of-state').dataset.watching) || 0;
      const ok = await confirmModal({
        title: 'Turn off the IPTV output?',
        message: 'No IPTV player can connect until it is turned on again.',
        consequence: n ? `${players(n)} watching right now ${n === 1 ? 'is' : 'are'} cut.` : '',
        confirmLabel: 'Turn off',
      });
      if (!ok) return;
    }
    await post(FIELD_URL, { path: 'integrations.output_feed.enabled', value: to === 'on' });
    await refresh();
    if (to === 'on') showToast('The IPTV output is on.');
  }

  async function setRow(input) {
    const tr = input.closest('tr');
    const name = tr.dataset.name;
    const on = input.checked;
    const watchers = Number(tr.dataset.watching) || 0;
    if (!on && watchers) {
      const upNext = tr.dataset.upNext === '1';
      const ok = await confirmModal({
        title: upNext ? `Turn off ${name}?` : `Hide ${name}?`,
        message: `${players(watchers)} ${watchers === 1 ? 'is' : 'are'} watching it right now, and ${upNext ? 'turning it off' : 'hiding it'} stops that play.`,
        confirmLabel: upNext ? 'Turn off' : 'Hide',
      });
      if (!ok) { input.checked = true; return; }
    }
    try {
      const d = await post(`/api/iptv-output/rows/${tr.dataset.feed}`, { on });
      await refresh(CARDS);
      const cut = d.cut ? ` ${players(d.cut)} watching it ${d.cut === 1 ? 'was' : 'were'} cut.` : '';
      // An Up next channel is ChannelBin's own, in no TV Guide, and off until a person turns it on.
      if (tr.dataset.upNext === '1') {
        showToast(on ? `${name} is on the IPTV output. An IPTV player shows it the next time it refreshes its channel list.`
          : `${name} is off the IPTV output. An IPTV player drops it the next time it refreshes its channel list.${cut}`);
      } else if (on) showToast(`${name} is back on the IPTV output, as the same channel it was before.`);
      else {
        showToast(`${name} is hidden from the IPTV output and stays in your TV Guide. An IPTV player drops it the next time it refreshes its channel list.${cut}`);
      }
    } catch (err) {
      input.checked = !on;
      fail(err);
    }
  }

  async function setAccount(input) {
    const name = input.dataset.name;
    const allow = input.checked;
    try {
      const d = await post(`/api/iptv-output/accounts/${input.dataset.id}`, { allow });
      await refresh(CARDS);
      if (allow) showToast(`The IPTV output may play from ${name} again.`);
      else {
        showToast(`The IPTV output no longer plays from ${name}.${d.cut ? ` ${players(d.cut)} playing from it ${d.cut === 1 ? 'was' : 'were'} cut.` : ''}`,
          { type: d.cut ? 'warning' : 'success' });
      }
    } catch (err) {
      input.checked = !allow;
      fail(err);
    }
  }

  async function setHeld(select) {
    const n = Number(select.value);
    const name = select.dataset.name;
    try {
      await post(`/api/iptv-output/accounts/${select.dataset.id}`, { held: n });
      await refresh(CARDS);
      showToast(n ? `Saved. Recordings now leave ${n} connection${n === 1 ? '' : 's'} of ${name} alone.`
        : `Saved. Recordings may use every connection of ${name}.`);
    } catch (err) {
      fail(err);
      refresh(CARDS);
    }
  }

  async function stopPlay(btn) {
    const ok = await confirmModal({
      title: 'Stop this play?',
      message: `The IPTV player at ${btn.dataset.client} stops playing ${btn.dataset.name} and its connection is free again.`,
      consequence: 'Nothing stops the player from tuning in again. To keep it off a channel, hide the channel.',
      confirmLabel: 'Stop',
      danger: true,
    });
    if (!ok) return;
    try {
      await post(`/api/iptv-output/plays/${encodeURIComponent(btn.dataset.id)}/stop`);
    } catch (err) {
      fail(err);
    }
    refresh(CARDS);
  }

  // The Settings card: each row saves its own key through the Settings page's own route,
  // which is the one validator of these keys.
  async function saveSetting(el) {
    const path = el.dataset.settingPath;
    let value = el.value;
    if (el.type === 'number') {
      value = Math.round(Number(el.value));
      if (!Number.isFinite(value) || el.value.trim() === '') { refresh(); return; }
      if (el.min !== '') value = Math.max(Number(el.min), value);
      if (el.max !== '') value = Math.min(Number(el.max), value);
    }
    const mode = path.endsWith('.mode');
    try {
      await post(FIELD_URL, { path, value });
      el.value = value;
      await refresh(mode ? ALL : STATE_ONLY);
      if (mode) {
        const said = {
          proxy: 'Saved. Plays now go through ChannelBin.',
          mixed: 'Saved. Channel groups and sports channels now go through ChannelBin, and the rest straight to your IPTV provider.',
          redirect: 'Saved. An IPTV player is now sent straight to your IPTV provider.',
        };
        if (!(value in said)) throw new Error(`Unknown feed mode "${value}"`);
        showToast(said[value]);
      } else showToast(`Saved. ${el.dataset.label}: ${value}.`);
    } catch (err) {
      fail(err);
      refresh();   // the field goes back to what is stored
    }
  }

  // Stored as the version the page was handed, so a release with a new notice shows it again.
  async function hideAddressNotice(btn) {
    await jsonFetch(`/api/user-prefs/${btn.dataset.pref}`, {
      method: 'POST',
      body: JSON.stringify({ value: Number(btn.dataset.version) }),
    });
    refresh(CARDS);
  }

  /* ---- Picture-in-picture channels (static/js/pip-setup.js is the setup dialog) ---------- */
  const reloads = 'An IPTV player shows it the next time it reloads its channel list.';
  const cutNote = (n) => (n ? ` ${players(n)} watching it ${n === 1 ? 'was' : 'were'} cut.` : '');
  const changingNote = (n) => (n ? ` ${players(n)} watching it ${n === 1 ? 'sees' : 'see'} the change within a few seconds.` : '');
  const keptNote = (n) => (n ? ` ${players(n)} watching it ${n === 1 ? 'keeps' : 'keep'} the picture ${n === 1 ? 'it has' : 'they have'} until ${n === 1 ? 'it tunes' : 'they tune'} in again.` : '');

  async function pipSaved(d, creating) {
    await refresh(CARDS);
    if (creating) {
      showToast(d.num ? `${d.name} is on the output as channel ${d.num}${d.swapped_num ? `, and its swapped copy as ${d.swapped_num}` : ''}. ${reloads}`
        : `${d.name} is created. It is on the output once the IPTV output is turned on.`);
    } else {
      showToast(d.kept || d.changing ? `Saved.${changingNote(d.changing)}${keptNote(d.kept)}${cutNote(d.cut)}` : `Saved. ${d.name} shows it the next time a TV tunes in.${cutNote(d.cut)}`);
    }
  }

  const pipSetup = (tr) => guarded(openPipSetup)(tr ? JSON.parse(tr.dataset.spec) : null, { onSaved: guarded(pipSaved) });

  async function pipSwap(tr) {
    const d = await post(`/api/iptv-output/pip/${tr.dataset.pip}/swap`);
    await refresh(CARDS);
    showToast(`Swapped the two pictures of ${d.name}.${changingNote(d.changing)}${keptNote(d.kept)}`);
  }

  async function pipClear(tr) {
    const name = tr.dataset.name;
    const ok = await confirmModal({
      title: `Clear ${name}?`,
      message: `${name} stays on the output with nothing set up, and a TV on it gets a card saying so.`,
      confirmLabel: 'Clear',
    });
    if (!ok) return;
    const d = await post(`/api/iptv-output/pip/${tr.dataset.pip}/clear`);
    await refresh(CARDS);
    showToast(`${name} is cleared.${d.moved ? ` ${players(d.moved)} watching it ${d.moved === 1 ? 'is' : 'are'} being moved to its card.` : ''}${cutNote(d.cut)}`);
  }

  async function pipRemove(tr) {
    const name = tr.dataset.name;
    const twin = tr.querySelector('[data-act="pip-twin"]').checked;
    const n = Number(tr.dataset.watching) || 0;
    const ok = await confirmModal({
      title: `Remove ${name} from the output?`,
      message: `${name}${twin ? ' and its swapped copy leave' : ' leaves'} the channel list IPTV players get, and what it was set up to show is deleted.`,
      consequence: `A player still shows it until it reloads its channel list.${n ? ` ${players(n)} watching it right now ${n === 1 ? 'is' : 'are'} cut.` : ''}`,
      confirmLabel: 'Remove',
      danger: true,
    });
    if (!ok) return;
    const d = await jsonFetch(`/api/iptv-output/pip/${tr.dataset.pip}`, { method: 'DELETE' });
    await refresh(CARDS);
    showToast(`${name} is removed from the output.${cutNote(d.cut)}`);
  }

  async function pipTwin(input) {
    const tr = input.closest('tr');
    const on = input.checked;
    try {
      const d = await post(`/api/iptv-output/pip/${tr.dataset.pip}/swapped-copy`, { on });
      await refresh(CARDS);
      showToast(on ? (d.swapped_num ? `${tr.dataset.twin} is on the output as channel ${d.swapped_num}. ${reloads}` : `${tr.dataset.twin} is offered once ${tr.dataset.name} is on the output.`)
        : `${tr.dataset.twin} left the output.${cutNote(d.cut)}`);
    } catch (err) {
      input.checked = !on;
      fail(err);
    }
  }

  /* ---- Logins (dev/changelog/1760, 1781) ------------------------------------------------- */
  const loginPassword = (tr) => passwordOf(tr.dataset.login);
  // A username suggested from the name while the person has not typed one of their own: what
  // login_problem() takes, kept short for a remote.
  const suggestUser = (name) => name.toLowerCase().replace(/[^a-z0-9]+/g, '').slice(0, 20);
  const loginCutNote = (n) => (n ? ` ${n === 1 ? 'Its play was' : `Its ${n} plays were`} ended.` : '');

  function openLoginForm(tr) {
    const editing = Boolean(tr);
    const card = $('#of-logins');
    const min = Number(card.dataset.min);
    const max = Number(card.dataset.max);
    const checked = (on) => (on ? ' checked' : '');
    const body = `<div class="of-login"><p>${editing
      ? 'A new username or password cuts any play on this login now, and its IPTV player is refused until the new one is typed into it.'
      : 'The server address goes into the IPTV player with this username and password. Short is easier to type with a remote, but a login used from outside your home network should be long: the password filled in here is.'}</p>
      <label for="of-l-name">Who it is for</label>
      <input class="form-control" id="of-l-name" value="${escHtml(editing ? tr.dataset.name : '')}" autocomplete="off" maxlength="100">
      <label for="of-l-user">Username</label>
      <input class="form-control" id="of-l-user" value="${escHtml(editing ? tr.dataset.user : '')}" autocomplete="off" maxlength="60">
      <label for="of-l-pass">Password</label>
      <input class="form-control" id="of-l-pass" autocomplete="off" maxlength="60" value="${editing ? '' : escHtml(card.dataset.password)}"${editing ? ' placeholder="Leave empty to keep the password it has"' : ''}>
      <label for="of-l-streams">Streams at once</label>
      <input class="form-control" id="of-l-streams" type="number" min="${min}" max="${max}" value="${editing ? tr.dataset.streams : 1}">
      <p class="card-note">Letters and numbers only keeps it easy to type on a TV. The password is kept as typed, so this page can show it back to you.</p></div>
      <label class="check-row"><input type="checkbox" id="of-l-direct"${checked(editing && tr.dataset.direct)}> <span>Plays straight from your IPTV provider at home
        <span class="text-muted">In redirect and mixed mode its IPTV player is sent to your provider's own stream, which hands it your provider login. Only for your own players. On the public port it always plays through ChannelBin.</span></span></label>
      <label class="check-row"><input type="checkbox" id="of-l-whole"${checked(editing && tr.dataset.whole)}> <span>Tell its IPTV player the whole reason a play is refused
        <span class="text-muted">The reason can name your pages, settings and folders. Off, its player is told only what kind of refusal it was, and the whole reason is still under Recent plays. On the public port it is always told only the kind.</span></span></label>`;
    const overlay = buildModal({
      title: editing ? `Change ${tr.dataset.name}'s login` : 'Add a login',
      body,
      footer: [
        { label: 'Cancel', class: 'btn' },
        { label: editing ? 'Save' : 'Add', class: 'btn btn-primary', onClick: (close) => {
          const data = {
            name: $('#of-l-name', overlay).value.trim(),
            username: $('#of-l-user', overlay).value.trim(),
            max_streams: Number($('#of-l-streams', overlay).value),
            provider_direct: $('#of-l-direct', overlay).checked,
            full_refusals: $('#of-l-whole', overlay).checked,
          };
          const password = $('#of-l-pass', overlay).value;
          if (password || !editing) data.password = password;
          const url = editing ? `/api/iptv-output/logins/${tr.dataset.login}` : '/api/iptv-output/logins';
          post(url, data).then((d) => {
            close();
            // The first login brings the other cards with it.
            return refresh(d.first ? ALL : CARDS).then(() => showToast(editing
              ? `Saved.${loginCutNote(d.ended)}`
              : d.first ? `Login added for ${data.name}. Turn the output on when you are ready.`
                : `Login added for ${data.name}. Copy the details to send from its menu.`));
          }).catch(fail);
          return false;
        } },
      ],
    });
    if (!editing) {
      const name = $('#of-l-name', overlay);
      const user = $('#of-l-user', overlay);
      name.addEventListener('input', () => { if (!user.dataset.typed) user.value = suggestUser(name.value); });
      user.addEventListener('input', () => { user.dataset.typed = '1'; });
    }
  }

  async function setLoginOn(input) {
    const tr = input.closest('tr');
    const on = input.checked;
    try {
      const d = await post(`/api/iptv-output/logins/${tr.dataset.login}`, { enabled: on });
      await refresh(CARDS);
      showToast(on ? `${tr.dataset.name} can log in again.` : `${tr.dataset.name} can no longer log in.${loginCutNote(d.ended)}`,
        { type: !on && d.ended ? 'warning' : 'success' });
    } catch (err) {
      input.checked = !on;
      fail(err);
    }
  }

  async function deleteLogin(tr) {
    const n = Number(tr.dataset.playing) || 0;
    const last = $$('#of-logins tr[data-login]').length === 1;
    const ok = await confirmModal({
      title: `Delete ${tr.dataset.name}'s login?`,
      message: `The username ${tr.dataset.user} stops working, and its IPTV player is refused the next time it asks for anything.`,
      consequence: `Its lines in Recent plays stay.${n ? ` ${players(n)} watching on it right now ${n === 1 ? 'is' : 'are'} cut.` : ''}${last ? ' It is the last login, so the IPTV output is turned off too.' : ''} To stop it for a while instead, turn it off.`,
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    const d = await jsonFetch(`/api/iptv-output/logins/${tr.dataset.login}`, { method: 'DELETE' });
    // The last login takes the other cards with it, as the first one brings them.
    await refresh(d.last ? ALL : CARDS);
    showToast(`${tr.dataset.name}'s login is deleted.${loginCutNote(d.ended)}${d.turned_off
      ? ' It was the last login, so the IPTV output is turned off until you add one and turn it on.' : ''}`);
  }

  async function revealLogin(btn) {
    const tr = btn.closest('tr');
    const value = $('[data-secret-text]', tr);
    const shown = btn.dataset.shown === '1';
    if (value.dataset.masked === undefined) value.dataset.masked = value.textContent;
    value.textContent = shown ? value.dataset.masked : await loginPassword(tr);
    btn.dataset.shown = shown ? '' : '1';
    btn.innerHTML = icon(shown ? 'reveal' : 'conceal');
    btn.setAttribute('aria-label', `${shown ? 'Show' : 'Hide'} the password`);
  }

  async function copyLogin(tr) {
    const password = await loginPassword(tr);
    const server = $('#of-logins').dataset.server;
    await copyText(`Server address: ${server}\nUsername: ${tr.dataset.user}\nPassword: ${password}\n`
      + 'Add it in your IPTV player as an Xtream Codes account.');
    showToast(`The details for ${tr.dataset.name} are copied. They hold the password, so send them only to that person.`);
  }

  /* ---- A login's channels (dev/changelog/1764) ------------------------------------------- */
  async function chooseChannels(tr) {
    const d = await jsonFetch(`/api/iptv-output/logins/${tr.dataset.login}/channels`);
    const picked = new Set(d.picked);
    // Grouped by category in the output's own order, which is the order a player lists them.
    const byCat = new Map();
    d.rows.forEach((r) => {
      if (!byCat.has(r.category)) byCat.set(r.category, []);
      byCat.get(r.category).push(r);
    });
    // A search matches the category too, since a provider's category names its channels.
    const rowHtml = (r) => `<label class="check-row" data-hay="${escHtml(`${r.name} ${r.category}`.toLowerCase())}">`
      + `<input type="checkbox" value="${r.id}"${picked.has(r.id) ? ' checked' : ''}> <span>${escHtml(r.name)}`
      + `${r.swapped ? ' <span class="text-muted">and its swapped copy</span>' : ''}`
      + `${r.off ? ` <span class="text-warning">${escHtml(r.off)}</span>` : ''}</span></label>`;
    const list = Array.from(byCat, ([cat, rows]) => `<div data-lu-cat><div class="of-lu-head">${escHtml(cat)}</div>`
      + `${rows.map(rowHtml).join('')}</div>`).join('');
    // Not inside .of-login, whose block labels would undo .check-row's line.
    const body = `<div>
      <label class="fp-radio"><input type="radio" name="of-lu-mode" value="all"${d.everything ? ' checked' : ''}> Everything on the output
        <span class="text-muted">Every channel you offer, and any you add to the output later.</span></label>
      <label class="fp-radio"><input type="radio" name="of-lu-mode" value="pick"${d.everything ? '' : ' checked'}> Only the channels I pick
        <span class="text-muted">A channel added to the output later is not added here. A picture-in-picture channel shows its two pictures whatever channels they come from.</span></label>
      <div class="of-lu-pick"${d.everything ? ' hidden' : ''}>
        <div class="of-lu-tools"><input class="search" type="search" placeholder="Search channels" aria-label="Search channels">
          <button class="btn btn-sm" type="button" data-lu="all">Pick all shown</button><button class="btn btn-sm" type="button" data-lu="none">Clear all shown</button>
          <span class="of-lu-n"></span></div>
        <div class="of-lu-list">${list || '<p class="card-note">The output has no channels yet.</p>'}</div>
      </div>
      <p class="card-note">Their IPTV player shows the change the next time it loads its channel list. A play of a channel you take out is cut now.</p></div>`;
    const overlay = buildModal({
      title: `Channels for ${tr.dataset.name}`,
      body,
      footer: [
        { label: 'Cancel', class: 'btn' },
        { label: 'Save', class: 'btn btn-primary', onClick: (close) => {
          const all = $('input[name="of-lu-mode"]:checked', overlay).value === 'all';
          const ids = boxes().filter((b) => b.checked).map((b) => Number(b.value));
          if (!all && !ids.length) {
            showToast('Pick at least one channel, or turn the login off.', { type: 'error' });
            return false;
          }
          post(`/api/iptv-output/logins/${tr.dataset.login}`, { lineup: all ? null : ids }).then((res) => {
            close();
            // No count here: the card counts what the player lists, which a picture-in-picture
            // pick (two channels) and a pick off the output make differ from the ticks.
            return refresh(CARDS).then(() => showToast(`${all ? `${tr.dataset.name} gets every channel on the output.`
              : `${tr.dataset.name} gets the channels you picked.`}${loginCutNote(res.ended)}`));
          }).catch(fail);
          return false;
        } },
      ],
    });
    const boxes = () => $$('.of-lu-list input[type="checkbox"]', overlay);
    const shown = () => boxes().filter((b) => !b.closest('label').hidden);
    const count = () => {
      const n = boxes().filter((b) => b.checked).length;
      $('.of-lu-n', overlay).textContent = `${n} picked`;
    };
    $$('input[name="of-lu-mode"]', overlay).forEach((r) => r.addEventListener('change', () => {
      $('.of-lu-pick', overlay).hidden = r.value === 'all';
    }));
    $('.of-lu-tools .search', overlay).addEventListener('input', (e) => {
      const q = e.target.value.trim().toLowerCase();
      $$('[data-lu-cat]', overlay).forEach((cat) => {
        let any = false;
        $$('label[data-hay]', cat).forEach((label) => {
          label.hidden = Boolean(q) && !label.dataset.hay.includes(q);
          any = any || !label.hidden;
        });
        cat.hidden = !any;
      });
    });
    $$('[data-lu]', overlay).forEach((btn) => btn.addEventListener('click', () => {
      shown().forEach((b) => { b.checked = btn.dataset.lu === 'all'; });
      count();
    }));
    $('.of-lu-list', overlay).addEventListener('change', count);
    count();
  }

  /* ---- A login's accounts (dev/changelog/1765) ------------------------------------------- */
  async function chooseAccounts(tr) {
    const d = await jsonFetch(`/api/iptv-output/logins/${tr.dataset.login}/accounts`);
    const picked = new Set(d.picked);
    const rowHtml = (a) => `<label class="check-row"><input type="checkbox" value="${a.id}"${picked.has(a.id) ? ' checked' : ''}> `
      + `<span>${escHtml(a.name)}${a.on_output ? '' : ' <span class="text-muted">Not one of the output\'s accounts</span>'}</span></label>`;
    const body = `<div>
      <label class="fp-radio"><input type="radio" name="of-la-mode" value="output"${d.own ? '' : ' checked'}> The IPTV output's accounts
        <span class="text-muted">The ones switched on in the Accounts card, and whatever that list becomes later.</span></label>
      <label class="fp-radio"><input type="radio" name="of-la-mode" value="pick"${d.own ? ' checked' : ''}> Only the accounts I pick
        <span class="text-muted">These take the place of the output's list for this login, so one the output leaves out can be picked here. Its IPTV player does not list picture-in-picture channels: their pictures are shared by everyone watching and come from the output's accounts.</span></label>
      <div class="of-la-pick"${d.own ? '' : ' hidden'}>${d.accounts.map(rowHtml).join('') || '<p class="card-note">There are no accounts yet.</p>'}</div>
      <p class="card-note">Their IPTV player shows the change the next time it loads its channel list. A play from an account you take out is cut now. A recording still takes a connection from any of these plays when it needs one.</p></div>`;
    const overlay = buildModal({
      title: `Accounts for ${tr.dataset.name}`,
      body,
      footer: [
        { label: 'Cancel', class: 'btn' },
        { label: 'Save', class: 'btn btn-primary', onClick: (close) => {
          const output = $('input[name="of-la-mode"]:checked', overlay).value === 'output';
          const ids = $$('.of-la-pick input[type="checkbox"]', overlay).filter((b) => b.checked).map((b) => Number(b.value));
          if (!output && !ids.length) {
            showToast("Pick at least one account, or use the IPTV output's accounts.", { type: 'error' });
            return false;
          }
          post(`/api/iptv-output/logins/${tr.dataset.login}`, { accounts: output ? null : ids }).then((res) => {
            close();
            return refresh(CARDS).then(() => showToast(`${output ? `${tr.dataset.name} plays from the IPTV output's accounts.`
              : `${tr.dataset.name} plays only from the accounts you picked.`}${loginCutNote(res.ended)}`));
          }).catch(fail);
          return false;
        } },
      ],
    });
    $$('input[name="of-la-mode"]', overlay).forEach((r) => r.addEventListener('change', () => {
      $('.of-la-pick', overlay).hidden = r.value === 'output';
    }));
  }

  async function clearLockout(btn) {
    const address = btn.dataset.address;
    const d = await post('/api/iptv-output/lockouts/clear', { address });
    await refresh(CARDS);
    showToast(d.cleared
      ? `${address} can try again now. A player still holding a wrong password is locked out again after a few more tries.`
      : `${address} was not locked out any more.`);
  }

  const guarded = (fn) => (...args) => Promise.resolve(fn(...args)).catch(fail);

  document.addEventListener('click', (e) => {
    const t = e.target.closest('[data-act]');
    if (!t || t.tagName === 'INPUT' || t.tagName === 'SELECT') return;
    const act = t.dataset.act;
    if (act === 'copy') guarded(copyRow)(t);
    else if (act === 'reveal') guarded(revealAddress)(t);
    else if (act === 'power') guarded(setPower)(t.dataset.to);
    else if (act === 'stop') stopPlay(t);
    else if (act === 'move-play') movePlay(t).then((asked) => { if (asked) refresh(CARDS); });
    else if (act === 'live-now') goLiveNow(t).then((changed) => { if (changed) refresh(); });
    else if (act === 'channel-now') channelNow(t, e);
    else if (act === 'preview') previewFrom(t);
    else if (act === 'tab') { view.tab = t.dataset.tab; applyTab(); }
    else if (act === 'show') { view.show = t.dataset.show; view.page = 1; applyFilter(); }
    else if (act === 'page') { view.page += Number(t.dataset.dir); applyFilter(); }
    else if (act === 'hide-address-notice') guarded(hideAddressNotice)(t);
    else if (act === 'pip-add') pipSetup(null);
    else if (act === 'pip-setup') pipSetup(t.closest('tr'));
    else if (act === 'pip-swap') guarded(pipSwap)(t.closest('tr'));
    else if (act === 'pip-clear') guarded(pipClear)(t.closest('tr'));
    else if (act === 'pip-remove') guarded(pipRemove)(t.closest('tr'));
    else if (act === 'login-add') openLoginForm(null);
    else if (act === 'login-edit') openLoginForm(t.closest('tr'));
    else if (act === 'login-delete') guarded(deleteLogin)(t.closest('tr'));
    else if (act === 'login-reveal') guarded(revealLogin)(t);
    else if (act === 'login-copy') guarded(copyLogin)(t.closest('tr'));
    else if (act === 'login-channels') guarded(chooseChannels)(t.closest('tr'));
    else if (act === 'login-accounts') guarded(chooseAccounts)(t.closest('tr'));
    else if (act === 'lockout-clear') guarded(clearLockout)(t);
  });
  document.addEventListener('change', (e) => {
    const t = e.target;
    if (t.dataset.act === 'row') setRow(t);
    else if (t.dataset.act === 'account') setAccount(t);
    else if (t.dataset.act === 'held') setHeld(t);
    else if (t.dataset.act === 'pip-twin') pipTwin(t);
    else if (t.dataset.act === 'login-on') setLoginOn(t);
    else if (t.dataset.act === 'conn-login') { view.login = t.value; applyLogin(); }
    else if (t.dataset.settingPath) saveSetting(t);
    else if (t.id === 'of-cat') { view.cat = t.value; view.page = 1; applyFilter(); }
  });
  document.addEventListener('input', (e) => {
    if (e.target.id !== 'of-q') return;
    view.q = e.target.value;
    view.page = 1;
    applyFilter();
  });

  // Recent plays' chips and pager are links, so a new-tab click works. A plain click puts the
  // link in the address bar and swaps only that card, through the same queue as every other
  // swap: the card has one updater, and a timed swap can never land after it with the old
  // filter. (bindHistoryChips swaps outside any queue, which is why it is not used here.)
  document.addEventListener('click', (e) => {
    const link = e.target.closest('#of-recent a[data-hist], #of-recent a[data-hist-more]');
    if (!link || wantsNewTab(e)) return;
    e.preventDefault();
    window.history.replaceState(null, '', link.href);
    swap(['#of-recent']).catch(() => window.location.reload());
  });
  setInterval(liveTick, LIVE_MS);
  document.addEventListener('visibilitychange', liveTick);
  applyView();
})();
