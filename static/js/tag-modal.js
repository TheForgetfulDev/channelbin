/* Shared create/edit modal for Tags (templates/tags.html), replacing the standalone
   /tags/new + /tags/<id>/edit form pages - the same move dev/changelog/356 made for the two
   profile types, and for the same reason: those pages were still on the pre-redesign
   .page-header / .form-card chrome.

   It deliberately does NOT extend profile-modal.js's spec engine. That engine exists to
   express one idea - "leave blank to inherit the global default" - across a list of scalar
   fields, and a tag has no global default to inherit from. What a tag has instead is two
   controls that engine has no concept of: a repeating list of match patterns, and a colour
   picker. Bending a spec engine around two one-off controls buys nothing and makes both
   callers harder to read, so this copies the PATTERN (buildModal + fieldRow + jsonFetch +
   showToast, pure helpers at file top level) rather than the code.

   Two things about it are load-bearing:

   1. **The client-side checks are presentation, not enforcement.** app/routes/tags.py runs
      the same rules and is what actually protects the row (CLAUDE.md: enforcement lives
      server-side). Notably the duplicate-name check exists ONLY there, because it is a
      question about the database - the modal surfaces that error when it arrives rather
      than keeping a second copy of the row set in step.
   2. **A tag is referenced by NAME, never by id** ({tag:live} in a filename template,
      the guide's tag filter). So renaming one is not a cosmetic edit, and the delete
      confirm in tags.js names every place the old name is spelled out.

   The pure helpers below are declared at FILE TOP LEVEL on purpose, the same way
   profile-modal.js's are: tests/test_tag_modal_js.py evaluates this file in node and calls
   them directly. Nothing test-only lives here - no module.exports tail, no injected globals.

   Depends on util.js (escHtml, jsonFetch, showToast, buildModal, fieldRow).
*/

const TAG_DEFAULT_COLOR = '#58a6ff';

/* Trim, drop empties, drop duplicates, keep order. Mirrors _clean_patterns in
   app/routes/tags.py - the server cleans the list again on arrival, so a stray blank row
   left in the DOM is never an error the user has to fix by hand. */
function tagCleanPatterns(raw) {
  const seen = [];
  (raw || []).forEach((p) => {
    const v = String(p == null ? '' : p).trim();
    if (v && !seen.includes(v)) seen.push(v);
  });
  return seen;
}

/* The stored form of a name. Lowercased because {tag:NAME} placeholders resolve against
   the stored value and the name column is unique - letting "Live" and "live" both exist
   would make the placeholder ambiguous. */
function tagNormalizeName(raw) {
  return String(raw == null ? '' : raw).trim().toLowerCase();
}

/* First failing rule's message, or null. Mirrors _read_tag_body in app/routes/tags.py.
   The name character class is \p{L}\p{N} rather than [a-z0-9] to match Python's
   Unicode-aware str.isalnum(): these tags exist to catch stylized Unicode markers, so an
   ASCII-only client check would refuse names the server accepts. */
function tagValidate(values) {
  const name = tagNormalizeName(values.name);
  if (!name) return 'Name is required.';
  const bare = name.replace(/[-_]/g, '');
  if (!bare || !/^[\p{L}\p{N}]+$/u.test(bare)) {
    return 'Name may only contain letters, numbers, hyphens, and underscores ' +
      '(it is used in filename template placeholders).';
  }
  if (!tagCleanPatterns(values.patterns).length) {
    return 'At least one match pattern is required.';
  }
  if (!/^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(String(values.color || ''))) {
    return 'Color must be a hex value like #58a6ff.';
  }
  return null;
}

/* The request body. */
function tagPayload(values) {
  return {
    name: tagNormalizeName(values.name),
    color: String(values.color || TAG_DEFAULT_COLOR),
    patterns: tagCleanPatterns(values.patterns),
  };
}

function tagPatternRowHtml(value) {
  return '<div class="pattern-row">' +
    `<input type="text" class="tag-pattern" value="${escHtml(value || '')}" placeholder="e.g. ᴸᶦᵛᵉ">` +
    '<button type="button" class="btn btn-sm tag-pattern-remove" aria-label="Remove pattern" ' +
    'title="Remove">&#x2715;</button>' +
    '</div>';
}

/* opts: { tag, presetColors, onDone } - tag omitted for the create flow. */
function openTagModal(opts) {
  const tag = opts.tag || null;
  const presets = opts.presetColors || [];
  const onDone = opts.onDone || (() => window.location.reload());
  const patterns = (tag && tag.patterns && tag.patterns.length) ? tag.patterns : [''];

  const body = document.createElement('div');
  body.innerHTML =
    '<div class="notice notice-info">' +
    (tag
      ? 'Renaming a tag does not update the places that spell out its name - a ' +
        '<code>{tag:' + escHtml(tag.name) + '}</code> placeholder in a filename template ' +
        'keeps referring to the old name until you change it too.'
      : 'A tag is a name, a colour, and the literal text it looks for. Matching is a ' +
        'case-insensitive substring test, and a tag matches if <em>any</em> of its ' +
        'patterns is found.') +
    '</div>' +
    fieldRow({
      label: 'Name', stack: true,
      meta: 'Lowercase letters, numbers, hyphens and underscores. This is the value used ' +
        'in filename template placeholders - <code>{tag:name}</code> and <code>{!tag:name}</code>.',
      control: `<input type="text" id="tag-name" value="${escHtml(tag ? tag.name : '')}" ` +
        'placeholder="e.g. live, new">',
    }) +
    fieldRow({
      label: 'Match patterns', stack: true,
      meta: 'The literal text this tag matches. Add more than one if different providers ' +
        'stylize the same marker differently.',
      control: `<div id="tag-patterns">${patterns.map(tagPatternRowHtml).join('')}</div>` +
        '<button type="button" class="btn btn-sm" id="tag-pattern-add">+ Add Pattern</button>',
    }) +
    fieldRow({
      label: 'Color', stack: true,
      meta: 'Used to highlight matching programs in the TV Guide.',
      control: colorPickerHtml('tag-', tag ? tag.color : TAG_DEFAULT_COLOR, TAG_DEFAULT_COLOR,
        presets.map(([hex, label]) => ({ hex, label }))),
    });

  const $ = (sel) => body.querySelector(sel);
  const readColor = wireColorPicker(body, 'tag-', TAG_DEFAULT_COLOR);

  $('#tag-pattern-add').addEventListener('click', () => {
    const rows = $('#tag-patterns');
    rows.insertAdjacentHTML('beforeend', tagPatternRowHtml(''));
    rows.lastElementChild.querySelector('input').focus();
  });

  // The last row is emptied rather than removed: a control that can render zero inputs
  // leaves the user with nothing to type into and no obvious way back.
  $('#tag-patterns').addEventListener('click', (e) => {
    const btn = e.target.closest('.tag-pattern-remove');
    if (!btn) return;
    const rows = $('#tag-patterns');
    if (rows.children.length > 1) btn.closest('.pattern-row').remove();
    else btn.closest('.pattern-row').querySelector('input').value = '';
  });

  const read = () => ({
    name: $('#tag-name').value,
    color: readColor(),
    patterns: Array.from(body.querySelectorAll('.tag-pattern')).map((i) => i.value),
  });

  function submit(close) {
    const current = read();
    const error = tagValidate(current);
    if (error) { showToast(error, { type: 'error' }); return; }
    const payload = tagPayload(current);
    jsonFetch(tag ? `/api/tags/${tag.id}` : '/api/tags', {
      method: tag ? 'PUT' : 'POST',
      body: JSON.stringify(payload),
    }).then(() => {
      showToast(`Tag "${payload.name}" saved.`);
      close();
      onDone();
    }).catch((err) => showToast(err.message, { type: 'error' }));
  }

  const modal = buildModal({
    title: tag ? `Edit "${tag.name}"` : 'Add a tag',
    panelClass: 'modal-wide',
    body,
    footer: [
      { label: 'Cancel', class: 'btn', onClick: (c) => c() },
      { label: tag ? 'Save' : 'Add tag', class: 'btn btn-primary',
        onClick: (close) => { submit(close); return false; } },
    ],
  });
  $('#tag-name').focus();
  return modal;
}
