"""Tier 0 - the shared list-page filter bar, driven in a real DOM.

`static/js/filter-bar.js` is the one implementation of DESIGN.md 3.11's "+ Filter" chip:
one control that opens a list of dimensions, each of which opens its own values, with
every chosen value coming back as a removable chip. Three pages drive it (the recordings
list, the Groups tab, the group detail member list), so a defect here is a defect on all
three at once - and none of what it promises is reachable from Python, because the server
renders an empty popover and an empty chip row, which is also exactly what a page whose
script threw on line one looks like.

The interesting half is shared with util.js and cannot be observed without both files in
one window: util.js closes an open menu when a `button.menu-item` inside it is clicked,
which is right for an action row and wrong for a filter, so "picking Fail then Warn is
one visit to the popover" is a fact about that handler being stopped. tests/support/
filter_bar.mjs therefore evaluates the shipped util.js the way base.html's <script src>
would have, and reports; every assertion lives here.

What it cannot cover: jsdom computes no layout, so where the popover lands, the viewport
clamp, and whether the chips wrap at 375px are browser work. Rollout: dev/changelog/767.

  python3 -m unittest tests.test_filter_bar_js
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'filter_bar.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    """Drive the bar once in node and return every scenario's observations."""
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    proc = subprocess.run([shutil.which('node'), HARNESS, REPO],
                          capture_output=True, text=True, cwd=REPO, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr}')
    _RESULT = json.loads(proc.stdout)
    return _RESULT


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class _Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()

    def test_the_page_booted_without_an_error(self):
        """Every assertion below is worthless if the script threw - an empty chip row is
        also what a bar that never ran looks like."""
        self.assertEqual(self.obs['errors'], [])


class NothingActiveTests(_Base):
    """Server-rendered initial state must equal the nothing-active state: JS may upgrade
    the page, it must never be required to calm one down (CLAUDE.md frontend rules)."""

    def test_no_chips_and_no_count_at_load(self):
        self.assertEqual(self.obs['initial']['chips'], [])
        self.assertEqual(self.obs['initial']['count'], 0)

    def test_every_row_is_visible_at_load(self):
        self.assertEqual(self.obs['initial']['rows'], ['a', 'b', 'c', 'd'])

    def test_the_popover_opens_on_the_dimension_list_not_on_values(self):
        self.assertEqual(self.obs['initial']['menu'], ['Status', 'Account'])

    def test_a_dimension_that_is_not_available_is_never_offered(self):
        """`available()` is how a facet gates a dimension - the account filter on a
        one-account list, a status filter on a group with no health check. A dimension
        offered anyway is a menu row that can only ever narrow the list to nothing."""
        self.assertNotIn('Never offered', self.obs['initial']['menu'])
        self.assertNotIn('Never offered', self.obs['after_back']['menu'])


class DrillInTests(_Base):
    """One popover, two levels: the dimensions, then one dimension's values."""

    def test_values_carry_the_count_of_rows_that_match_them(self):
        self.assertEqual(self.obs['drilled']['menu'], ['Pass2', 'Fail1', '<b>Warn</b>1'])

    def test_a_value_label_is_escaped(self):
        """Labels come from a registry that may carry provider-supplied text (an account
        name). An unescaped one is markup injection into the app's own popover."""
        self.assertIn('&lt;b&gt;Warn&lt;/b&gt;', self.obs['drilled_html'])
        self.assertNotIn('<b>Warn</b>', self.obs['drilled_html'])

    def test_back_returns_to_the_dimension_list(self):
        self.assertEqual(self.obs['after_back']['menu'], ['Status', 'Account'])

    def test_the_popover_stays_open_while_drilling(self):
        self.assertTrue(self.obs['drilled']['open'])
        self.assertTrue(self.obs['after_back']['open'])


class MatchSemanticsTests(_Base):
    """OR inside one dimension, AND across dimensions - the only sensible reading of
    "Status: Fail, Warn" beside "Account: A"."""

    def test_one_value_filters_the_list_and_leaves_one_chip(self):
        self.assertEqual(self.obs['one_value']['chips'], ['Status: Fail ✕'])
        self.assertEqual(self.obs['one_value']['rows'], ['c'])
        self.assertEqual(self.obs['one_value']['count'], 1)

    def test_a_second_value_in_the_same_dimension_widens_the_list(self):
        self.assertEqual(self.obs['or_within']['rows'], ['a', 'b', 'c'])

    def test_a_second_dimension_narrows_it(self):
        """Rows b and c are the only ones that are both (PASS or FAIL) and account 2."""
        self.assertEqual(self.obs['and_across']['rows'], ['b', 'c'])

    def test_the_popover_stays_open_after_a_value_is_picked(self):
        """util.js dismisses a menu when a `button.menu-item` inside it is clicked, which
        is right for an action and wrong here: choosing two statuses must be one visit,
        the way the checkbox list this component replaced behaved."""
        self.assertTrue(self.obs['one_value']['open'])
        self.assertTrue(self.obs['or_within']['open'])

    def test_a_chosen_value_is_ticked_in_the_popover(self):
        """A drilled-in dimension whose chosen values look identical to its unchosen ones
        cannot say what is already on."""
        self.assertIn('✓Fail1', self.obs['one_value']['menu'])


class ChipTests(_Base):

    def test_clicking_a_chip_removes_exactly_that_filter(self):
        self.assertEqual(self.obs['chip_removed']['chips'],
                         ['Status: Pass ✕', 'Account: Account 2 ✕'])
        self.assertEqual(self.obs['chip_removed']['rows'], ['b'])

    def test_a_chip_names_its_dimension_and_its_value(self):
        """"Fail" alone is ambiguous the moment a second dimension has a value by that
        name; the chip is the only thing on screen saying which filter is on."""
        self.assertEqual(self.obs['and_across']['chips'][2], 'Account: Account 2 ✕')


class InvisibleFilterTests(_Base):
    """The one state this must never come to rest in: a filter still hiding rows from
    behind a chip that is no longer on screen."""

    def test_a_value_that_stops_existing_is_dropped_with_its_chip(self):
        self.assertEqual(self.obs['before_prune']['chips'], ['Account: Account 1 ✕'])
        self.assertEqual(self.obs['after_prune']['chips'], [])
        self.assertEqual(self.obs['after_prune']['count'], 0)

    def test_the_rows_it_was_hiding_come_back(self):
        """Not merely the chip: the filter itself has to stop applying, or the list is
        narrowed by something with no control anywhere that can widen it again."""
        self.assertEqual(self.obs['after_prune']['rows'], ['b', 'c', 'd'])

    def test_a_dimension_that_stops_being_offered_takes_its_filter_with_it(self):
        self.assertEqual(self.obs['before_unoffer']['rows'], ['a'])
        self.assertEqual(self.obs['after_unoffer']['chips'], [])
        self.assertEqual(self.obs['after_unoffer']['rows'], ['a', 'b', 'c', 'd'])
        self.assertEqual(self.obs['after_unoffer']['menu'], ['Status'])


class RoundTripTests(_Base):
    """entries()/setFrom() are what a URL-persisted bar rides on (the Groups tab)."""

    def test_entries_are_flat_key_value_pairs(self):
        self.assertEqual(self.obs['and_across']['entries'],
                         [['status', 'FAIL'], ['status', 'PASS'], ['account', '2']])

    def test_set_from_restores_a_saved_selection(self):
        self.assertEqual(self.obs['restored']['chips'],
                         ['Status: Fail ✕', 'Account: Account 2 ✕'])
        self.assertEqual(self.obs['restored']['rows'], ['c'])

    def test_a_key_no_dimension_owns_is_ignored(self):
        """A URL outlives a registry: a bookmarked filter naming a dimension that has
        since been renamed must not become a predicate nothing can satisfy."""
        self.assertEqual(self.obs['restored']['count'], 2)


class OnChangeTests(_Base):

    def test_the_caller_is_notified_once_per_user_change(self):
        """Four value/chip clicks in the scenario above; render() and setFrom() are not
        changes and must not fire it, or a caller that redraws on change recurses."""
        self.assertEqual(self.obs['changes'], 4)


if __name__ == '__main__':
    unittest.main()
