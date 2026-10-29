'use strict';

// The group page's What's On card (dev/changelog/1324, mockup 46): every distinct EPG
// schedule a group's channels carry, lined up under the group's own row and tinted where it
// differs. guide.js draws the grid in its `schedules` embed mode - one timeline scale, one
// grid - and calls the sched* functions here for the parts only this card has: its rows, its
// channel column, the agreement on each cell, the outlines and bands, and its own controls.
// Loaded after guide.js; every function here runs after DOMContentLoaded.
//
// Nothing here decides agreement. The server ran each schedule through
// epg_sources.compare_listings() against the group's row and sent the verdict and each
// program's agreement; this file only draws them.

// duplicated from app/epg_sources.py (CMP_*, AGREE_*, GAP_*) - the payload's own words,
// pinned equal to the Python constants by tests/test_group_whats_on.py.
const SCHED_VERDICTS = {
  IDENTICAL: 'identical', DESCRIPTIONS: 'descriptions', MOSTLY: 'mostly',
  SHIFTED: 'shifted', UNRELATED: 'unrelated', NO_OVERLAP: 'no-overlap',
};
const SCHED_AGREE = {
  SAME: 'same', MOVED: 'moved', DIFFERENT: 'different', ONLY_HERE: 'only-here',
  DOUBLED: 'doubled', OUTSIDE: 'outside', PAST: 'past', REFERENCE: 'reference',
};
const SCHED_GAP = { REFERENCE: 'reference', OTHER: 'other' };

// The compare-sources page's verdict words and badge classes
// (templates/channels/guide_compare.html), with one change: here the comparator's "Nothing
// to compare" can only mean the schedule has no listings, because the group's row is
// gap-filled from every channel in the group (fill_listings), so it covers any time any
// schedule lists.
const SCHED_VERDICT = {
  [SCHED_VERDICTS.IDENTICAL]: { cls: 'b-done', word: 'Identical', short: 'Identical',
    says: 'The same programs at the same times as the guide.' },
  [SCHED_VERDICTS.DESCRIPTIONS]: { cls: 'b-done', word: 'Identical, descriptions differ', short: 'Identical',
    says: 'The same programs at the same times as the guide. Some have a description in one and not the other.' },
  [SCHED_VERDICTS.MOSTLY]: { cls: 'b-sched', word: 'Mostly the same', short: 'Mostly',
    says: 'Nothing moved, but a few programs differ from the guide or appear on one side only.' },
  [SCHED_VERDICTS.SHIFTED]: { cls: 'b-warn', word: 'Shifted', short: 'Shifted',
    says: 'The guide\'s programs, at different times.' },
  [SCHED_VERDICTS.UNRELATED]: { cls: 'b-fail', word: 'Unrelated', short: 'Unrelated',
    says: 'Fewer than half of the guide\'s programs have a match here. This is another channel\'s schedule.' },
  [SCHED_VERDICTS.NO_OVERLAP]: { cls: 'b-abort', word: 'No listings', short: 'None',
    says: 'This source holds no listings for this id from now on, so there is nothing to compare.' },
};
const SCHED_SAME = new Set([SCHED_VERDICTS.IDENTICAL, SCHED_VERDICTS.DESCRIPTIONS]);
const SCHED_TINTED = new Set([SCHED_AGREE.MOVED, SCHED_AGREE.DIFFERENT, SCHED_AGREE.ONLY_HERE,
  SCHED_AGREE.DOUBLED]);
const SCHED_GAP_WORDS = {
  [SCHED_GAP.REFERENCE]: 'Description only in the guide',
  [SCHED_GAP.OTHER]: 'Description only here',
};

// Channel-column and outline line heights, from guide.js's CELL_LINE so the two cannot
// drift: badge, id and the count/source line on desktop; a phone hides the third line.
const SCHED_CHAN_H = CELL_LINE.title + 2 + CELL_LINE.subtitle + 2 + CELL_LINE.meta + CELL_LINE.padV;

// The last payload, and the card's two controls. Neither control is saved: the card opens
// on All / All every time. The fold is Layout state and lives in guide.js's LAYOUT.
const SCHED = { data: null, guideRow: null, chan: 'all', show: 'all', folded: [], all: [] };

function schedPayload() {
  return (SCHED.data && SCHED.data.schedules) || { rows: [], no_epg_id: [], records: false };
}

// ── What is on screen ────────────────────────────────────────────────────────

// Called by guide.js with each fetch. Returns the rows to draw, which become channelData.
function schedLoad(data) {
  SCHED.data = data;
  SCHED.guideRow = (data.channels || [])[0] || null;
  return schedVisibleRows();
}

// The schedules on screen, after the Channels control (which changes members), the fold,
// and the Show control - in that order, each named. Each becomes a row shaped like the
// group's own, so the program chooser and the record dialog treat a cell on it exactly as
// a cell on the group's row: the recording is the group's.
function schedVisibleRows() {
  if (!SCHED.guideRow) { SCHED.all = []; SCHED.folded = []; return []; }
  const rec = SCHED.chan === 'rec';
  const all = schedPayload().rows.map((s, i) => ({
    s, i, shown: rec ? s.members.filter(m => m.recording_enabled) : s.members,
  })).filter(x => x.shown.length);
  const fold = !!LAYOUT.fold_same && SCHED.show !== 'same';
  const folded = fold ? all.filter(x => SCHED_SAME.has(x.s.verdict)) : [];
  let rows = fold ? all.filter(x => !SCHED_SAME.has(x.s.verdict)) : all;
  switch (SCHED.show) {
    case 'all': break;
    case 'same': rows = rows.filter(x => SCHED_SAME.has(x.s.verdict)); break;
    case 'different': rows = rows.filter(x => !SCHED_SAME.has(x.s.verdict)); break;
    default: console.error(`guide-schedules: unknown Show state ${SCHED.show}`);
  }
  SCHED.all = all;
  SCHED.folded = folded;
  const g = SCHED.guideRow;
  return [Object.assign({}, g, { sched: null })].concat(rows.map(x => Object.assign({}, g, {
    id: `s${x.i}`, name: x.s.epg_id, programs: x.s.programs, recordings: [],
    sched: x.s, shown: x.shown,
  })));
}

// The column and the empty program rows for channelData, laid down before guide.js fills
// them. Rebuilt on every render, since the controls change which rows exist.
function schedBuildRows() {
  channelData = schedVisibleRows();
  const col = document.getElementById('guide-channel-col');
  const rows = document.getElementById('guide-program-rows');
  if (!col || !rows) return;
  col.innerHTML = '<div class="sched-corner">Schedule</div>' + channelData.map(ch =>
    (ch.sched ? schedCellHtml(ch) : schedGuideCellHtml())).join('');
  rows.innerHTML = channelData.map(ch =>
    `<div class="guide-row${ch.sched ? '' : ' sched-guide-row'}" data-channel-id="${escHtml(String(ch.id))}"></div>`).join('');
  channelData.forEach(ch => {
    ch.doubledStarts = new Set((ch.programs || [])
      .filter(p => p.agreement === SCHED_AGREE.DOUBLED).map(p => p.start_time));
  });
}

// ── Row height (guide.js asks) ───────────────────────────────────────────────

function schedColumnHeight() {
  return isMobileGuide() ? 0 : SCHED_CHAN_H;
}

// An outline always carries its "not listed here" line, whatever the time fields say.
function schedOutlineHeight() {
  const any = channelData.some(ch => ch.sched && ch.sched.missing.length);
  return any ? CELL_LINE.title + CELL_LINE.gap + CELL_LINE.meta : 0;
}

// ── Words ────────────────────────────────────────────────────────────────────

function schedTip(lines) { return lines.filter(Boolean).join('\n'); }
function schedDate(iso) { return utcIsoToDate(iso); }
function schedDayTime(iso) {
  const d = schedDate(iso);
  return `${fmtDateTz(d, { weekday: 'short' })} ${fmtTimeTz(d)}`;
}
function schedSpan(a, b) { return `${schedDayTime(a)} - ${fmtTimeTz(schedDate(b))}`; }
function schedHm(mins) {
  const m = Math.abs(mins);
  const h = Math.floor(m / 60);
  if (!h) return counted(m, 'minute');
  return m % 60 ? `${h}h ${m % 60}m` : counted(h, 'hour');
}
function schedSource(s) { return s.source_name || 'No EPG source'; }

function schedGapSentence(gap) {
  switch (gap) {
    case SCHED_GAP.REFERENCE: return 'The guide\'s listing of it has a description; this one has none.';
    case SCHED_GAP.OTHER: return 'This listing has a description; the guide\'s has none.';
  }
  console.error(`guide-schedules: unknown description side ${gap}`);
  return '';
}

function schedAgreementLine(p) {
  switch (p.agreement) {
    case SCHED_AGREE.SAME:
      return p.gap ? `Same as the guide. ${schedGapSentence(p.gap)}` : 'Same as the guide.';
    case SCHED_AGREE.MOVED: {
      const d = Math.round((schedDate(p.start_time) - schedDate(p.partner.start_time)) / 60000);
      return `Moved: the guide has it at ${schedDayTime(p.partner.start_time)}, ` +
        `${schedHm(d)} ${d > 0 ? 'earlier' : 'later'}.`;
    }
    case SCHED_AGREE.DIFFERENT:
      return `Different: the guide has "${p.partner.title}" here.`;
    case SCHED_AGREE.ONLY_HERE:
      return 'Only on this schedule: the guide has nothing starting within 5 minutes of it.';
    case SCHED_AGREE.DOUBLED:
      return 'Listed twice: this source gives two programs the same start time. Not counted in the verdict.';
    case SCHED_AGREE.OUTSIDE:
      return 'Not compared: past the end of what both schedules list.';
    case SCHED_AGREE.PAST:
      return 'Already over. The comparison starts from now.';
    case SCHED_AGREE.REFERENCE: {
      const serving = schedPayload().serving_name;
      return p.listings_from
        ? `Borrowed from ${p.listings_from}: ${serving}'s guide has nothing here.`
        : `From ${serving}, the channel this group records from.`;
    }
  }
  console.error(`guide-schedules: unknown agreement ${p.agreement}`);
  return '';
}

function schedCountsLine(s) {
  const c = s.counts;
  if (s.verdict === SCHED_VERDICTS.NO_OVERLAP) return SCHED_VERDICT[s.verdict].says;
  const parts = [`${c.same} the same`];
  if (c.moved) parts.push(`${c.moved} moved`);
  if (c.different) parts.push(`${c.different} different`);
  if (c.only_here) parts.push(`${c.only_here} only here`);
  if (c.only_guide) parts.push(`${c.only_guide} missing`);
  let line = `Of ${counted(c.compared, 'program')} compared: ${parts.join(', ')}.`;
  if (c.doubled) line += ` ${counted(c.doubled, 'listing')} doubled, not counted.`;
  const gaps = { [SCHED_GAP.REFERENCE]: 0, [SCHED_GAP.OTHER]: 0 };
  s.programs.forEach(p => { if (p.gap) gaps[p.gap] += 1; });
  if (gaps[SCHED_GAP.REFERENCE]) line += ` ${gaps[SCHED_GAP.REFERENCE]} described only in the guide.`;
  if (gaps[SCHED_GAP.OTHER]) line += ` ${gaps[SCHED_GAP.OTHER]} described only here.`;
  return line;
}

// Where the group's row starts borrowing this schedule's listings, if it does: the row's
// cells name the channel a borrowed listing came from, and a channel is on one schedule.
function schedBorrowStart(s) {
  const names = new Set(s.members.map(m => m.name));
  const p = (SCHED.guideRow.programs || []).find(x => x.listings_from && names.has(x.listings_from));
  return p ? p.start_time : null;
}

function schedMemberLines(s, shown) {
  const lines = shown.map(m => `${m.name} - ${m.account_name}${m.recording_enabled ? '' : ' (Recording off)'}`);
  const hidden = s.members.length - shown.length;
  if (hidden) lines.push(`and ${counted(hidden, 'channel')} with Recording off, hidden by the filter`);
  return lines;
}

function schedVerdictBadge(s) {
  if (s.lead) return `<span class="badge b-sched">${isMobileGuide() ? 'Guide' : 'In the guide'}</span>`;
  const v = SCHED_VERDICT[s.verdict];
  return `<span class="badge ${v.cls}">${escHtml(isMobileGuide() ? v.short : v.word)}</span>`;
}

// ── The channel column ───────────────────────────────────────────────────────

function schedCellTip(ch) {
  const s = ch.sched;
  const v = SCHED_VERDICT[s.verdict];
  const p = schedPayload();
  const borrow = schedBorrowStart(s);
  return schedTip([
    s.lead ? `${s.epg_id} - in the guide` : `${s.epg_id} - ${v.word}`,
    schedSource(s),
    s.lead ? `The group's row shows this schedule first: ${p.pinned_name || p.serving_name} leads it.` : v.says,
    s.lead ? '' : schedCountsLine(s),
    borrow ? `The guide borrows this schedule's listings from ${schedDayTime(borrow)}.` : '',
    s.spellings.length > 1 ? `Also spelled ${s.spellings.slice(1).join(', ')} - one id, since matching ignores case.` : '',
    s.split ? 'These channels read the same id but hold different listings, so they have a row of their own.' : '',
    '', ...schedMemberLines(s, ch.shown), '', 'Click for the details.',
  ]);
}

// The badge has its line to itself on desktop, since the longest verdict ("Identical,
// descriptions differ") fills the column; the count is said under the id. A phone hides
// that line, so the bare count rides beside the short verdict there instead.
function schedCellHtml(ch) {
  const s = ch.sched;
  const count = ch.shown.length === s.members.length
    ? counted(ch.shown.length, 'channel')
    : `${ch.shown.length} of ${counted(s.members.length, 'channel')}`;
  return `<div class="guide-channel-row" data-sched="${escHtml(ch.id)}" data-tip="${escHtml(schedCellTip(ch))}">` +
    '<div class="sched-cell">' +
    `<div class="sched-cell-top">${schedVerdictBadge(s)}<span class="sched-count">${ch.shown.length}</span></div>` +
    `<div class="sched-id">${escHtml(s.epg_id)}</div>` +
    `<div class="sched-meta">${escHtml(`${count} · ${schedSource(s)}`)}</div></div></div>`;
}

function schedGuideCellHtml() {
  const p = schedPayload();
  const lead = p.rows.find(s => s.lead);
  const folded = SCHED.folded;
  const t = schedTip([
    p.records ? 'What the TV Guide shows for this group'
      : 'What the TV Guide would show for this group',
    p.records
      ? `The group's row records from ${p.serving_name}${p.pinned_name ? `, and takes its listings from ${p.pinned_name} first (pinned)` : ''}.`
      : 'No channel has Recording on, so the group has no guide row and nothing records from it. This is what the row would hold.',
    p.borrowed && p.borrowed.length ? `Where its guide ends, the rest is borrowed from ${p.borrowed.join(', ')}.` : '',
    'Every row below is compared against this one.',
    ...(folded.length ? ['', 'The same as the guide, folded into this row:',
      ...folded.map(x => `${x.s.epg_id} (${schedSource(x.s)}) - ${SCHED_VERDICT[x.s.verdict].word}`)] : []),
  ]);
  const fold = folded.length ? `<span class="sched-fold">+${folded.length} the same</span>` : '';
  return `<div class="guide-channel-row is-group" data-sched="guide" data-tip="${escHtml(t)}">` +
    '<div class="sched-cell">' +
    `<div class="sched-cell-top"><span class="sched-guide-lbl">TV Guide</span>${fold}</div>` +
    `<div class="sched-id">${escHtml(lead ? lead.epg_id : '')}</div>` +
    `<div class="sched-meta">${escHtml(p.serving_name ? `via ${p.serving_name}` : '')}</div></div></div>`;
}

// ── Cells (guide.js asks, per cell and per row) ──────────────────────────────

function schedDecorateCell(el, prog, ch) {
  el.classList.add(`sched-k-${prog.agreement}`);
  if (SCHED_TINTED.has(prog.agreement)) el.classList.add('sched-tinted');
  // The first of a doubled pair takes the row's top half, the second its bottom half.
  if (prog.agreement !== SCHED_AGREE.DOUBLED && ch.doubledStarts && ch.doubledStarts.has(prog.start_time)) {
    el.classList.add('sched-pair-top');
  }
  if (prog.gap) {
    const mark = document.createElement('span');
    mark.className = `sched-gapmark sched-gap-${prog.gap}`;
    mark.setAttribute('aria-label', SCHED_GAP_WORDS[prog.gap]);
    el.prepend(mark);
  }
  el.dataset.tip = schedTip([prog.title, prog.sub_title, schedSpan(prog.start_time, prog.stop_time),
    schedAgreementLine(prog), 'Click for the program chooser.']);
}

// A cell records from the group's own row, never from a channel the card happened to
// show - which is why `ch` here is a row shaped like the group's. A group nobody records
// from has no row in the guide to record from, so the cell says so instead.
function schedCellClick(prog, ch) {
  if (!schedPayload().records) {
    showToast('Nothing records from this group yet: no channel has Recording on. Turn ' +
      'Recording on for a channel in the Channels list, and its programs can be recorded here.',
    { type: 'info' });
    return;
  }
  openProgramChooser(prog, ch);
}

function schedPx(iso) {
  return ((schedDate(iso) - windowStart) / 60000) * PIXELS_PER_MINUTE;
}

// The outlines of guide programs a schedule lacks, and the band where it has nothing.
function schedRowExtras(row, ch, totalMin) {
  const s = ch.sched;
  if (!s) return;
  const endPx = totalMin * PIXELS_PER_MINUTE;
  s.missing.forEach((g, i) => {
    const left = Math.max(0, schedPx(g.start_time));
    const right = Math.min(endPx, schedPx(g.stop_time));
    if (right <= left) return;
    const width = Math.max(right - left - 2, 2);
    const el = document.createElement('div');
    el.className = `guide-program sched-ghost${width < 60 ? ' narrow' : ''}`;
    el.style.left = `${left}px`;
    el.style.width = `${width}px`;
    el.dataset.ghost = String(i);
    el.dataset.tip = schedTip([`Not on this schedule: ${g.title}`, schedSpan(g.start_time, g.stop_time),
      'The guide has this program, and this schedule lists nothing at this time.']);
    el.innerHTML = `<div class="guide-program-title">${escHtml(g.title)}</div>` +
      '<div class="guide-program-time">not listed here</div>';
    el.addEventListener('click', () => showToast('Nothing to open: this schedule has no ' +
      'program here. The outline is the guide\'s program, for comparison.', { type: 'info' }));
    row.appendChild(el);
  });
  let from = null;
  let text = '';
  if (!s.programs.length) {
    from = 0;
    text = `No listings for ${s.epg_id} in ${schedSource(s)}\nIts channels carry this id, ` +
      'but the source holds nothing for it in this window.';
  } else if (s.ends && schedDate(s.ends) < windowEnd) {
    from = schedPx(s.ends);
    text = `No listings after ${schedDayTime(s.ends)}\n${schedSource(s)} holds nothing for ` +
      `${s.epg_id} past this point.` + (s.lead ? ' The group\'s row borrows from the next channel from here on.' : '');
  }
  if (from === null || from >= endPx) return;
  const band = document.createElement('div');
  band.className = 'sched-band';
  band.style.left = `${Math.max(0, from)}px`;
  band.style.width = `${endPx - Math.max(0, from) - 2}px`;
  band.dataset.tip = text;
  band.innerHTML = `<span>${escHtml(text.split('\n')[0])}</span>`;
  row.appendChild(band);
}

// ── The status bar, summary and notes (guide.js asks, once per render) ───────

function schedSummaryHtml(all) {
  const channels = all.reduce((sum, x) => sum + x.shown.length, 0);
  const by = (v) => all.filter(x => x.s.verdict === v);
  const same = all.filter(x => SCHED_SAME.has(x.s.verdict)).length;
  const parts = [];
  if (same) parts.push(`<strong>${same}</strong> carry the guide's programs`);
  if (by(SCHED_VERDICTS.MOSTLY).length) parts.push(`<strong>${by(SCHED_VERDICTS.MOSTLY).length}</strong> mostly the same`);
  const shifted = by(SCHED_VERDICTS.SHIFTED);
  if (shifted.length) {
    const most = Math.max(...shifted.map(x => Math.abs(x.s.counts.largest_move_min || 0)));
    parts.push(`<strong>${shifted.length}</strong> shifted by up to ${escHtml(schedHm(most))}`);
  }
  if (by(SCHED_VERDICTS.UNRELATED).length) parts.push(`<strong>${by(SCHED_VERDICTS.UNRELATED).length}</strong> unrelated`);
  if (by(SCHED_VERDICTS.NO_OVERLAP).length) parts.push(`<strong>${by(SCHED_VERDICTS.NO_OVERLAP).length}</strong> with no listings`);
  const tail = parts.length > 1
    ? `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}` : parts.join('');
  return `<strong>${escHtml(counted(all.length, 'schedule'))}</strong> across ` +
    `${escHtml(counted(channels, 'channel'))}${tail ? `: ${tail}` : ''}.`;
}

function schedLegendHtml() {
  const items = [
    ['', 'Same as the guide'],
    ['sched-tinted sched-k-moved', 'Moved'],
    ['sched-tinted sched-k-different', 'Different program'],
    ['sched-tinted sched-k-only-here', 'Only on this schedule'],
    ['sched-ghost', 'Missing here'],
    ['sched-tinted sched-k-doubled', 'Listed twice'],
    ['sched-k-outside', 'Not compared'],
    [`sched-sw-gap sched-gap-${SCHED_GAP.REFERENCE}`, SCHED_GAP_WORDS[SCHED_GAP.REFERENCE]],
    [`sched-sw-gap sched-gap-${SCHED_GAP.OTHER}`, SCHED_GAP_WORDS[SCHED_GAP.OTHER]],
  ];
  return items.map(([cls, label]) =>
    `<span class="guide-legend-item"><span class="sched-sw ${cls}"></span>${escHtml(label)}</span>`).join('');
}

function schedEmptyHtml(rows) {
  if (rows) return '';
  switch (SCHED.show) {
    case 'different':
      return 'Every schedule here carries the guide\'s programs.<span class="guide-empty-sub">' +
        'The ids differ, the programming does not. Choose Show: All to see them.</span>';
    case 'same':
      return 'No schedule here matches the guide\'s.<span class="guide-empty-sub">Choose Show: All to see them.</span>';
    case 'all':
      return 'Every schedule is folded into the guide row.<span class="guide-empty-sub">' +
        'They all carry the guide\'s programs.</span>';
  }
  console.error(`guide-schedules: unknown Show state ${SCHED.show}`);
  return '';
}

function schedStatusBar() {
  const card = document.getElementById('sched-card');
  if (!card) return;
  card.querySelectorAll('[data-chan]').forEach(b => b.classList.toggle('active', b.dataset.chan === SCHED.chan));
  card.querySelectorAll('[data-show]').forEach(b => b.classList.toggle('active', b.dataset.show === SCHED.show));
  const p = schedPayload();
  const all = SCHED.all;
  const rows = channelData.length - 1;
  const channels = all.reduce((sum, x) => sum + x.shown.length, 0);
  let line = rows === all.length
    ? `${counted(all.length, 'schedule')} · ${counted(channels, 'channel')}`
    : `${rows} of ${counted(all.length, 'schedule')} shown · ${counted(channels, 'channel')}`;
  if (SCHED.folded.length) line += ` · ${SCHED.folded.length} folded into the guide row`;
  const set = (id, fn) => { const el = document.getElementById(id); if (el) fn(el); };
  set('sched-counts', el => { el.textContent = line; });
  set('sched-head-count', el => { el.textContent = counted(p.rows.length, 'schedule'); });
  set('sched-legend', el => { el.innerHTML = schedLegendHtml(); });
  set('sched-summary', el => { el.innerHTML = all.length ? schedSummaryHtml(all) : ''; });
  set('sched-empty', el => {
    const html = SCHED.guideRow ? schedEmptyHtml(rows > 0) : '';
    el.hidden = !html;
    el.innerHTML = html;
  });
  set('sched-noid', el => {
    const missing = (p.no_epg_id || []).filter(m => SCHED.chan === 'all' || m.recording_enabled);
    el.hidden = !missing.length;
    el.textContent = missing.length
      ? `${missing.map(m => m.name).join(', ')} ${missing.length === 1 ? 'has' : 'have'} no EPG id, so ` +
        `${missing.length === 1 ? 'it has' : 'they have'} no row here. That is unknown, not mismatched.`
      : '';
  });
}

// ── The schedule's details: a modal on desktop, the bottom sheet on a phone ──

function schedOpenDetails(key) {
  const p = schedPayload();
  if (key === 'guide') {
    const body = (p.records
      ? `<p>The TV Guide's row for this group records from <strong>${escHtml(p.serving_name)}</strong>` +
        `${p.pinned_name ? ` and takes its listings from <strong>${escHtml(p.pinned_name)}</strong> first` : ''}, ` +
        'then fills any time those lack from the next channel by rank.</p>'
      : '<p>No channel in this group has Recording on, so it has no row in the TV Guide and nothing ' +
        'records from it. This row is what the guide would show: the best-ranked channel\'s ' +
        'listings, filled from the next channel by rank.</p>') +
      (p.borrowed && p.borrowed.length
        ? `<p class="text-muted">Borrowing from: ${escHtml(p.borrowed.join(', '))}.</p>` : '') +
      (SCHED.folded.length
        ? '<div class="sched-detail-h">The same as the guide</div><ul class="sched-detail-members">' +
          SCHED.folded.map(x => `<li><span class="sched-mname">${escHtml(x.s.epg_id)} - ${escHtml(schedSource(x.s))}</span>` +
            `<span class="badge ${SCHED_VERDICT[x.s.verdict].cls}">${escHtml(SCHED_VERDICT[x.s.verdict].word)}</span></li>`).join('') +
          '</ul>' : '');
    openSheet({ title: 'TV Guide row', body });
    return;
  }
  const ch = channelData.find(c => String(c.id) === key);
  if (!ch || !ch.sched) return;
  const s = ch.sched;
  const v = SCHED_VERDICT[s.verdict];
  const borrow = schedBorrowStart(s);
  const badge = s.lead ? '<span class="badge b-sched">In the guide</span>'
    : `<span class="badge ${v.cls}">${escHtml(v.word)}</span>`;
  const body = `<p class="sched-detail-verdict">${badge}${escHtml(s.lead
    ? 'The group\'s row shows this schedule first.' : v.says)}</p>` +
    `<p class="text-muted">${escHtml(schedSource(s))}${s.spellings.length > 1
      ? ` · also spelled ${escHtml(s.spellings.slice(1).join(', '))}` : ''}</p>` +
    (s.split ? '<p class="text-muted">These channels read the same id as another row but hold different listings.</p>' : '') +
    (s.lead ? '' : `<p>${escHtml(schedCountsLine(s))}</p>`) +
    (borrow ? `<p>The guide borrows this schedule's listings from ${escHtml(schedDayTime(borrow))}.</p>` : '') +
    `<div class="sched-detail-h">${escHtml(counted(ch.shown.length, 'channel'))}</div><ul class="sched-detail-members">` +
    ch.shown.map(m => `<li><a href="${escHtml(`/channels/${m.id}`)}" class="sched-mname">${escHtml(m.name)}</a>` +
      `<span class="text-muted small">${escHtml(m.account_name)}</span>` +
      `<span class="badge ${m.recording_enabled ? 'b-done' : 'b-abort'}">${m.recording_enabled ? 'Recording on' : 'Recording off'}</span></li>`).join('') +
    '</ul>';
  openSheet({ title: s.epg_id, body });
}

// The mobile Layout sheet's body for this card (guide.js's openLayoutSheet asks): the fold,
// then the cell fields this card offers, in the user's order.
function schedLayoutSheetHtml(cb, fieldRows) {
  return '<h4>Rows</h4>' +
    cb('fold_same', 'Fold schedules the same as the guide into its row') +
    '<h4>Program cell fields</h4>' + fieldRows +
    '<p class="guide-sheet-hint">Fields render in this order. Descriptions are not shown in ' +
    'grid cells at this width - tap a program to read one. Saved for this card only: the TV ' +
    'Guide and the channel page\'s What\'s On keep their own layouts.</p>';
}

// ── The card's own controls ──────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  const card = document.getElementById('sched-card');
  if (!card) return;
  card.addEventListener('click', e => {
    const chan = e.target.closest('[data-chan]');
    if (chan) { SCHED.chan = chan.dataset.chan; renderGuide(); return; }
    const show = e.target.closest('[data-show]');
    if (show) { SCHED.show = show.dataset.show; renderGuide(); }
  });
});
