"""Tier 0 - Escape closes the innermost overlay, not everything at once (static/js/util.js).

Guards dev/docs/BUGS.md 2026-08-03 @ "Escape inside a menu opened from a modal closed the
modal too".

util.js and buildModal both listen for Escape on `document`. That was harmless while every
menu in the app lived on a page, because a page has no modal handler to collide with. The
filename designer (dev/changelog/441) is the first surface to open a dropdown from INSIDE a
modal, and there both handlers fired on one keypress: the menu closed and the modal closed
under it, losing everything typed into the designer.

stopPropagation cannot fix this - two listeners on the same node both run regardless - so
the menu handler must call stopImmediatePropagation, and it must be the one registered
first. Both of those are structural properties this file pins, because neither is visible
from reading either handler alone.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTIL_JS = os.path.join(REPO, 'static', 'js', 'util.js')


def _read():
    with open(UTIL_JS, encoding='utf-8') as fh:
        return fh.read()


class MenuEscapeTests(unittest.TestCase):
    def _handler(self, src):
        """The menu's own keydown listener, from its registration to the matching brace."""
        start = src.index("document.addEventListener('keydown'", src.index('function closeMenus'))
        return src[start:start + 700]

    def test_the_menu_handler_stops_the_remaining_listeners_on_document(self):
        """stopPropagation would not help: buildModal's handler is on the SAME node."""
        body = self._handler(_read())
        self.assertIn('stopImmediatePropagation', body)
        self.assertNotIn('e.stopPropagation()', body)

    def test_it_only_swallows_escape_when_a_menu_is_actually_open(self):
        """Swallowing Escape unconditionally would make a modal with no menu open
        undismissable - the opposite defect, and a worse one."""
        body = self._handler(_read())
        self.assertIn(".querySelector('.menu.open')", body)
        # The guard must come BEFORE the swallow, or it is not a guard.
        self.assertLess(body.index(".querySelector('.menu.open')"),
                        body.index('stopImmediatePropagation'))

    def test_the_menu_handler_is_registered_before_buildModal_can_add_its_own(self):
        """Order decides which listener wins, and this one only wins because util.js binds
        it at load time while buildModal binds its own when a modal opens. A refactor that
        moved this registration inside buildModal, or after it, would silently restore the
        defect."""
        src = _read()
        menu_reg = src.index("document.addEventListener('keydown'",
                             src.index('function closeMenus'))
        build_modal = src.index('function buildModal')
        self.assertLess(menu_reg, build_modal,
                        'the menu Escape listener must be bound at load, before buildModal')

    def test_close_menus_is_still_the_one_close_path(self):
        """Every dismissal - Escape, outside click, scroll - goes through closeMenus so the
        scroll lock is synced once rather than per path."""
        src = _read()
        body = self._handler(src)
        self.assertIn('closeMenus()', body)
        self.assertIn('syncScrollLock', src[src.index('function closeMenus'):
                                            src.index('function positionMenu')])


if __name__ == '__main__':
    unittest.main()
