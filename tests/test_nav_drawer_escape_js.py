"""Tier 0 - Escape closes the mobile nav drawer, and only when the drawer is what Escape
should close (templates/base.html, static/js/util.js).

Guards dev/docs/BUGS.md 2026-08-05 @ "the mobile nav drawer could not be closed with
Escape"; shipped in dev/changelog/468.

Every other overlay in the app closes on Escape - buildModal binds its own handler, the
menus and the lightbox have theirs in util.js - and the drawer was the one that did not,
because its IIFE wired `setOpen()` to the hamburger, the scrim, the nav links and the
>900px resize close, and to no key at all.

The listener itself is three lines, so what actually needs pinning is the part that is not
visible from reading those three lines: the drawer is the OUTERMOST overlay on mobile, so
it must stand down whenever something is stacked over it. Two mechanisms do that, and both
are invisible locally - util.js's menu handler swallows Escape with stopImmediatePropagation
before this one runs (already pinned by tests/test_menu_escape_js.py), and everything else
is covered by `overlayOpenAboveDrawer()`. Without the second, one Escape over an open modal
would close the modal AND the drawer under it, which is the same defect class the menu
handler was fixed for.

Python cannot reach any of this, so the drawer's IIFE is lifted out of the rendered template
and evaluated in node against a DOM stub, then real Escape events are dispatched at it - the
same arrangement tests/test_dropdown_js.py uses, one level up because this dispatches events
rather than calling pure functions. Nothing test-only lives in the shipped files.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_HTML = os.path.join(REPO, 'templates', 'base.html')
UTIL_JS = os.path.join(REPO, 'static', 'js', 'util.js')

# One node run answers every scenario: booting the IIFE is cheap, but shelling out is not,
# and it is the same script each time. Each class below reads its slice of the result.
_HARNESS = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[1], 'utf8');
const util = fs.readFileSync(process.argv[2], 'utf8');

// The drawer's IIFE, identified by the one element only it touches.
function drawerSource() {
  const anchor = html.indexOf("getElementById('topnav-toggle')");
  if (anchor < 0) throw new Error('no topnav-toggle lookup in base.html');
  const start = html.lastIndexOf('(function', anchor);
  const end = html.indexOf('})();', anchor);
  if (start < 0 || end < 0) throw new Error('could not bound the drawer IIFE');
  return html.slice(start, end + 5);
}

// OVERLAY_SEL + the helper, lifted alone rather than by evaluating all of util.js.
function overlayHelperSource() {
  const sel = util.match(/const OVERLAY_SEL =[\s\S]*?;\n/);
  const fn = util.match(/function overlayOpenAboveDrawer\(\)[\s\S]*?\n}\n/);
  if (!sel) throw new Error('OVERLAY_SEL not found in util.js');
  if (!fn) throw new Error('overlayOpenAboveDrawer not found in util.js');
  return { source: sel[0] + fn[0], body: fn[0] };
}

function makeEl(classes) {
  const set = new Set(classes ? classes.split(' ') : []);
  return {
    _classes: set,
    innerHTML: '',
    listeners: {},
    classList: {
      contains: (n) => set.has(n),
      add: (n) => set.add(n),
      remove: (n) => set.delete(n),
      toggle: (n, force) => {
        const on = force === undefined ? !set.has(n) : !!force;
        if (on) set.add(n); else set.delete(n);
        return on;
      },
    },
    setAttribute() {},
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    querySelectorAll() { return []; },
  };
}

// Boot the drawer IIFE against a stub DOM, dispatch one keydown, report what moved.
function runDrawer({ open, above, key }) {
  const els = {
    'topnav-toggle': makeEl(''),
    'topnav-menu': makeEl('topnav-menu' + (open ? ' open' : '')),
    'menu-scrim': makeEl('menu-scrim' + (open ? ' open' : '')),
  };
  const docListeners = {};
  const document = {
    getElementById: (id) => els[id] || null,
    addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
    querySelectorAll() { return []; },
  };
  const window = { addEventListener() {}, innerWidth: 375 };
  let syncCalls = 0;
  let consulted = 0;
  const syncScrollLock = () => { syncCalls++; };
  const overlayOpenAboveDrawer = () => { consulted++; return !!above; };

  new Function('document', 'window', 'syncScrollLock', 'overlayOpenAboveDrawer',
               drawerSource())(document, window, syncScrollLock, overlayOpenAboveDrawer);

  const handlers = docListeners.keydown || [];
  handlers.forEach((fn) => fn({ key, stopImmediatePropagation() {}, stopPropagation() {} }));
  return {
    keydownHandlers: handlers.length,
    menuKeydownHandlers: (els['topnav-menu'].listeners.keydown || []).length,
    toggleKeydownHandlers: (els['topnav-toggle'].listeners.keydown || []).length,
    stillOpen: els['topnav-menu'].classList.contains('open'),
    scrimStillOpen: els['menu-scrim'].classList.contains('open'),
    syncCalls,
    consulted,
  };
}

// The helper itself, over a fake overlay population.
function runHelper(population) {
  const els = population.map(([cls, visible]) => ({
    classList: { contains: (n) => cls.split(' ').includes(n) },
    getClientRects: () => (visible ? [{}] : []),
  }));
  const seen = [];
  const document = { querySelectorAll(sel) { seen.push(sel); return els; } };
  const helper = overlayHelperSource();
  const fn = new Function('document', helper.source + '\nreturn overlayOpenAboveDrawer;')(document);
  return { result: fn(), selectorsQueried: seen };
}

const helper = overlayHelperSource();
console.log(JSON.stringify({
  closes: runDrawer({ open: true, above: false, key: 'Escape' }),
  standsDownForOverlay: runDrawer({ open: true, above: true, key: 'Escape' }),
  closedDrawer: runDrawer({ open: false, above: false, key: 'Escape' }),
  otherKey: runDrawer({ open: true, above: false, key: 'a' }),
  helperSeesModal: runHelper([['topnav-menu open', true], ['modal', true]]),
  helperIgnoresDrawer: runHelper([['topnav-menu open', true]]),
  helperIgnoresHiddenModal: runHelper([['topnav-menu open', true], ['modal', false]]),
  helperBody: helper.body,
}));
"""


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
class _Base(unittest.TestCase):
    _observed = None

    @classmethod
    def setUpClass(cls):
        if _Base._observed is None:
            proc = subprocess.run(['node', '-e', _HARNESS, BASE_HTML, UTIL_JS],
                                  capture_output=True, text=True, cwd=REPO, timeout=60)
            if proc.returncode != 0:
                raise AssertionError(f'node failed driving the drawer:\n{proc.stderr}')
            _Base._observed = json.loads(proc.stdout)
        cls.obs = _Base._observed


class DrawerEscapeTests(_Base):
    def test_escape_closes_the_open_drawer(self):
        """The whole point of the item: pressing Escape with the drawer open closes it."""
        r = self.obs['closes']
        self.assertGreaterEqual(r['keydownHandlers'], 1,
                                'no keydown listener is registered by the drawer IIFE')
        self.assertFalse(r['stillOpen'], 'Escape did not close the drawer')

    def test_closing_goes_through_setopen_so_the_scrim_and_scroll_lock_follow(self):
        """setOpen is the single mutator for the drawer, its scrim and the scroll lock. A
        handler that only stripped `.open` off the menu would leave the scrim dimming the
        page and the body pinned - the drawer would look closed and the page would be dead."""
        r = self.obs['closes']
        self.assertFalse(r['scrimStillOpen'], 'the scrim stayed open, so setOpen was bypassed')
        self.assertEqual(r['syncCalls'], 1,
                         'syncScrollLock ran %d times, expected exactly one via setOpen'
                         % r['syncCalls'])

    def test_the_listener_is_on_document_not_the_drawer(self):
        """A listener on the drawer or the toggle only fires when focus is already inside it,
        and nothing focuses the drawer on open - Escape would do nothing for a mouse user."""
        r = self.obs['closes']
        self.assertEqual(r['menuKeydownHandlers'], 0)
        self.assertEqual(r['toggleKeydownHandlers'], 0)

    def test_escape_stands_down_when_another_overlay_is_open(self):
        """Escape closes the innermost overlay. The drawer is the outermost one on mobile, so
        a modal stacked over it owns the key - closing both on one press is the defect
        tests/test_menu_escape_js.py exists for, one layer down."""
        r = self.obs['standsDownForOverlay']
        self.assertTrue(r['stillOpen'],
                        'the drawer closed underneath an overlay that was stacked over it')
        self.assertEqual(r['syncCalls'], 0)

    def test_escape_with_the_drawer_closed_does_nothing(self):
        """And short-circuits before consulting the overlay population - the open check is the
        cheap one and must come first, or every Escape anywhere in the app walks the DOM."""
        r = self.obs['closedDrawer']
        self.assertEqual(r['syncCalls'], 0)
        self.assertEqual(r['consulted'], 0,
                         'the handler queried the overlay population before checking whether '
                         'the drawer was even open')

    def test_only_escape_closes_it(self):
        """Typing into anything on the page must not dismiss the nav."""
        r = self.obs['otherKey']
        self.assertTrue(r['stillOpen'], 'a non-Escape key closed the drawer')
        self.assertEqual(r['syncCalls'], 0)


class OverlayAboveDrawerTests(_Base):
    def test_a_visible_overlay_counts(self):
        self.assertTrue(self.obs['helperSeesModal']['result'])

    def test_the_drawer_itself_does_not_count(self):
        """`.topnav-menu.open` is in OVERLAY_SEL, so without the exclusion the helper reports
        the drawer as stacked over itself and Escape can never close it."""
        self.assertFalse(self.obs['helperIgnoresDrawer']['result'])

    def test_a_hidden_overlay_does_not_count(self):
        """_record_modal.html's two hand-rolled modals sit in the DOM permanently, hidden by
        inline display. A bare selector match is therefore true on nearly every page, which
        would wedge Escape shut app-wide - visibility decides, exactly as it does in
        syncScrollLock."""
        self.assertFalse(self.obs['helperIgnoresHiddenModal']['result'])

    def test_it_derives_from_overlay_sel_rather_than_a_second_list(self):
        """One list of what counts as an overlay. A hand-written copy here drifts the first
        time an overlay is added to OVERLAY_SEL and only to OVERLAY_SEL."""
        self.assertIn('OVERLAY_SEL', self.obs['helperBody'])
        queried = self.obs['helperSeesModal']['selectorsQueried']
        self.assertEqual(len(queried), 1, 'the helper should query the DOM exactly once')
        for cls in ('.modal', '.topnav-menu.open'):
            self.assertIn(cls, queried[0],
                          f'{cls} is in OVERLAY_SEL but not in what the helper queried, so it '
                          'is querying something other than OVERLAY_SEL')


if __name__ == '__main__':
    unittest.main()
