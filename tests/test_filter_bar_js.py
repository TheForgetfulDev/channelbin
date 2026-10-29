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
class _Base:

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()

    def test_the_page_booted_without_an_error(self):
        """Every assertion below is worthless if the script threw - an empty chip row is
        also what a bar that never ran looks like."""
        self.assertEqual(self.obs['errors'], [])


class NothingActiveTests(_Base, unittest.TestCase):
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


class DrillInTests(_Base, unittest.TestCase):
    """One popover, two levels: the dimensions, then one dimension's values."""

    def test_values_carry_the_count_of_rows_that_match_them(self):
        self.assertEqual(self.obs['drilled']['menu'], ['Pass2', 'Fail1', '<b>Warn</b>1'])

    def test_a_value_label_is_escaped(self):
        """Labels come from a registry that may carry provider-supplied text (an account
        name). An unescaped one is markup injection into the app's own popover."""
        self.assertIn('&lt;b&gt;Warn&lt;/b&gt;', self.obs['drilled_html'])
        # Counted as elements rather than searched for as text: the label also rides in an
        # aria-label, and an HTML serializer writes `<` inside an attribute value verbatim
        # (it is inert there), so the string test would flag a safe attribute.
        self.assertEqual(self.obs['drilled_b_elements'], 0)

    def test_back_returns_to_the_dimension_list(self):
        self.assertEqual(self.obs['after_back']['menu'], ['Status', 'Account'])

    def test_the_popover_stays_open_while_drilling(self):
        self.assertTrue(self.obs['drilled']['open'])
        self.assertTrue(self.obs['after_back']['open'])


class MatchSemanticsTests(_Base, unittest.TestCase):
    """OR inside one dimension, AND across dimensions - the only sensible reading of
    "Status is Fail, Warn" beside "Account is A"."""

    def test_one_value_filters_the_list_and_leaves_one_chip(self):
        self.assertEqual(self.obs['one_value']['chips'], ['Status is Fail'])
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
        self.assertIn('[ok]Fail1', self.obs['one_value']['menu'])


class ChipTests(_Base, unittest.TestCase):

    def test_clicking_a_chip_removes_exactly_that_filter(self):
        self.assertEqual(self.obs['chip_removed']['chips'],
                         ['Status is Pass', 'Account is Account 2'])
        self.assertEqual(self.obs['chip_removed']['rows'], ['b'])

    def test_a_chip_names_its_dimension_and_its_value(self):
        """"Fail" alone is ambiguous the moment a second dimension has a value by that
        name; the chip is the only thing on screen saying which filter is on."""
        self.assertEqual(self.obs['and_across']['chips'][2], 'Account is Account 2')


class InvisibleFilterTests(_Base, unittest.TestCase):
    """The one state this must never come to rest in: a filter still hiding rows from
    behind a chip that is no longer on screen."""

    def test_a_value_that_stops_existing_is_dropped_with_its_chip(self):
        self.assertEqual(self.obs['before_prune']['chips'], ['Account is Account 1'])
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


class RoundTripTests(_Base, unittest.TestCase):
    """entries()/setFrom() are what a URL-persisted bar rides on (the Groups tab)."""

    def test_entries_are_flat_key_value_pairs(self):
        self.assertEqual(self.obs['and_across']['entries'],
                         [['status', 'FAIL'], ['status', 'PASS'], ['account', '2']])

    def test_set_from_restores_a_saved_selection(self):
        self.assertEqual(self.obs['restored']['chips'],
                         ['Status is Fail', 'Account is Account 2'])
        self.assertEqual(self.obs['restored']['rows'], ['c'])

    def test_a_key_no_dimension_owns_is_ignored(self):
        """A URL outlives a registry: a bookmarked filter naming a dimension that has
        since been renamed must not become a predicate nothing can satisfy."""
        self.assertEqual(self.obs['restored']['count'], 2)


class OnChangeTests(_Base, unittest.TestCase):

    def test_the_caller_is_notified_once_per_user_change(self):
        """Four value/chip clicks in the scenario above; render() and setFrom() are not
        changes and must not fire it, or a caller that redraws on change recurses."""
        self.assertEqual(self.obs['changes'], 4)



class _Saved(_Base):
    """Saved filters (opts.saved) - a named chip combination, loadable, one of which may be
    the default applied as the page opens (dev/changelog/1156)."""

    def setUp(self):
        self.s = self.obs['saved']
        for k in ('errors_a', 'errors_b', 'errors_c', 'errors_d', 'errors_e'):
            if any(str(e).startswith('threw:') for e in self.s.get(k, [])):
                self.fail(f'the saved-filter scenario threw: {self.s[k]}')

    def test_neither_saved_page_threw(self):
        for k in ('errors_a', 'errors_b', 'errors_c', 'errors_d', 'errors_e'):
            self.assertEqual(self.s[k], [], k)


class SavedFilterSaveTests(_Saved, unittest.TestCase):

    def test_nothing_saved_reads_plain(self):
        self.assertEqual(self.s['empty']['chip'], 'Saved')
        self.assertIn('Nothing saved yet', self.s['empty']['menu'])

    def test_an_empty_selection_or_an_empty_name_writes_nothing(self):
        """A saved filter with no chips is the unfiltered page under a name that promises
        otherwise; a nameless one cannot be picked out of the list."""
        self.assertEqual(self.s['save_nothing']['posts'], 0)
        self.assertEqual(self.s['save_nothing']['toasts'][-1][0], 'error')
        self.assertEqual(self.s['save_unnamed']['posts'], 0)
        self.assertEqual(self.s['save_unnamed']['toasts'][-1],
                         ['error', 'Give the filter a name first.'])

    def test_save_stores_the_chips_on_screen_and_names_them_on_the_chip(self):
        self.assertEqual(self.s['saved']['lastPost']['url'], '/api/user-prefs/test_saved')
        self.assertEqual(self.s['saved']['lastPost']['body']['value'],
                         [{'name': 'Failures', 'filters': [['status', 'FAIL']], 'is_default': False}])
        self.assertEqual(self.s['saved']['chip'], 'Saved: Failures')

    def test_changing_the_chips_marks_it_edited(self):
        """Derived from the chips, not a flag - so adding a value is enough to show it."""
        self.assertEqual(self.s['edited']['chip'], 'Saved: Failures (edited)')

    def test_saving_under_the_same_name_overwrites_and_says_so(self):
        self.assertEqual(self.s['overwritten']['saved'], ['Failures'])
        self.assertEqual(self.s['overwritten']['lastPost']['body']['value'][0]['filters'],
                         [['status', 'FAIL'], ['status', 'WARN']])
        self.assertEqual(self.s['overwritten']['toasts'][-1], ['success', 'Updated "Failures".'])
        self.assertEqual(self.s['overwritten']['chip'], 'Saved: Failures')


class SavedFilterLoadTests(_Saved, unittest.TestCase):

    def test_loading_replaces_the_chips_with_the_saved_ones(self):
        """Account: 1 was on the bar before the load; it must not survive into it."""
        self.assertEqual(self.s['loaded']['chips'], ['Status is FAIL', 'Status is WARN'])
        self.assertEqual(self.s['loaded']['rows'], ['c', 'd'])
        self.assertEqual(self.s['loaded']['chip'], 'Saved: Failures')

    def test_loading_is_an_action_and_closes_the_popover(self):
        self.assertFalse(self.s['loaded']['open'])


class SavedFilterDefaultTests(_Saved, unittest.TestCase):

    def test_set_default_marks_exactly_one_and_keeps_the_popover_open(self):
        self.assertEqual([r['is_default'] for r in self.s['default_one']['lastPost']['body']['value']],
                         [False, True])
        self.assertTrue(self.s['default_one']['stillOpen'])

    def test_setting_another_moves_the_default(self):
        self.assertEqual([r['is_default'] for r in self.s['default_moved']['lastPost']['body']['value']],
                         [True, False])

    def test_clear_default_leaves_none(self):
        self.assertEqual([r['is_default'] for r in self.s['default_cleared']['lastPost']['body']['value']],
                         [False, False])

    def test_the_default_is_on_the_bar_when_it_is_built(self):
        self.assertEqual(self.s['boot_default']['chips'],
                         ['Status is PASS', 'Account is Account 2'])
        self.assertEqual(self.s['boot_default']['rows'], ['b'])

    def test_a_saved_value_nothing_carries_is_dropped_and_named(self):
        """Dropping it is DESIGN.md 3.11; saying so is what stops a saved filter quietly
        showing more than its name promises."""
        kind, msg = self.s['boot_default']['toasts'][-1]
        self.assertEqual(kind, 'warning')
        self.assertIn('Status is GONE', msg)
        self.assertIn('"Pass on 2"', msg)
        self.assertEqual(self.s['boot_default']['chip'], 'Saved: Pass on 2 (edited)')

    def test_malformed_records_are_skipped_and_only_one_default_survives(self):
        self.assertEqual(self.s['boot_default']['saved'], ['Pass on 2 default', 'Second default'])
        self.assertEqual(self.s['boot_default']['badges'], 1)

    def test_building_the_bar_writes_nothing(self):
        self.assertEqual(self.s['boot_default']['posts'], 0)


class SavedFilterFailureTests(_Saved, unittest.TestCase):

    def test_a_failed_write_is_undone_on_screen_and_the_popover_says_so(self):
        self.assertEqual(self.s['delete_pending']['saved'], ['Failures'])
        self.assertEqual(self.s['delete_failed']['saved'], ['Failures', 'Account one'])
        self.assertIn('not saved', self.s['delete_failed']['err'])
        self.assertEqual(self.s['delete_failed']['toasts'][-1][0], 'error')

    def test_the_next_good_write_clears_the_error(self):
        self.assertEqual(self.s['deleted']['err'], '')

    def test_deleting_the_one_on_screen_stops_the_chip_naming_it(self):
        self.assertEqual(self.s['deleted']['saved'], ['Account one'])
        self.assertEqual(self.s['deleted']['chip'], 'Saved')



class IsNotTests(_Base, unittest.TestCase):
    """Every value is off, "is" or "is not" - the channel search's three-state rule and chip
    wording, lifted into the shared bar so the control means the same thing on every page
    (dev/changelog/1207). A dimension passes a row when it has no "is" values or one
    matches, AND none of its "is not" values match."""

    def test_an_excluded_value_hides_its_rows_and_only_those(self):
        self.assertEqual(self.obs['not_one']['rows'], ['a', 'b', 'd'])
        self.assertEqual(self.obs['not_one']['count'], 1)

    def test_its_chip_says_is_not_and_takes_the_negative_tint(self):
        self.assertEqual(self.obs['not_one']['chips'], ['Status is not Fail'])
        self.assertEqual(self.obs['not_one']['neg'], [True])

    def test_an_included_chip_does_not_take_the_negative_tint(self):
        self.assertEqual(self.obs['not_switched']['neg'], [False])

    def test_the_popover_lights_the_operator_that_is_on(self):
        fail = [o for o in self.obs['not_one']['ops'] if o['v'] == 'FAIL'][0]
        self.assertEqual((fail['is'], fail['not']), (False, True))
        self.assertIn('neg', fail['row'])
        others = [o for o in self.obs['not_one']['ops'] if o['v'] != 'FAIL']
        self.assertTrue(all(not o['is'] and not o['not'] for o in others))

    def test_the_popover_stays_open_while_choosing_an_operator(self):
        self.assertTrue(self.obs['not_one']['open'])

    def test_pressing_the_other_operator_switches_the_value_not_adds_it(self):
        """One value is never included and excluded at once - that pair matches nothing
        and would read as two contradictory chips."""
        self.assertEqual(self.obs['not_switched']['chips'], ['Status is Fail'])
        self.assertEqual(self.obs['not_switched']['rows'], ['c'])
        self.assertEqual(self.obs['not_switched']['entries'], [['status', 'FAIL']])

    def test_pressing_the_lit_operator_again_turns_the_value_off(self):
        self.assertEqual(self.obs['not_off']['chips'], [])
        self.assertEqual(self.obs['not_off']['rows'], ['a', 'b', 'c', 'd'])

    def test_tapping_an_excluded_row_presses_is_as_the_channel_search_does(self):
        """The row is the "is" control's bigger target, in both implementations - a tap that
        meant one thing here and another on the channel search is the drift this item
        exists to remove."""
        self.assertEqual(self.obs['not_row_off']['entries'], [['status', 'FAIL']])
        self.assertEqual(self.obs['not_row_off']['rows'], ['c'])

    def test_is_not_ands_across_dimensions(self):
        """Not FAIL, and account 2: b (PASS) and d (WARN)."""
        self.assertEqual(self.obs['not_and']['rows'], ['b', 'd'])
        self.assertEqual(self.obs['not_and']['entries'],
                         [['status', 'FAIL', 'not'], ['account', '2']])

    def test_clicking_an_is_not_chip_removes_it(self):
        self.assertEqual(self.obs['not_chip_removed']['chips'], ['Account is Account 2'])
        self.assertEqual(self.obs['not_chip_removed']['rows'], ['b', 'c', 'd'])

    def test_is_and_is_not_in_one_dimension_both_have_to_hold(self):
        """Tag is X and tag is not Y over a field a row carries several values of: only
        a, because b has X but also Y. An OR across the two would also let c and d in."""
        self.assertEqual(self.obs['not_mixed']['rows'], ['a'])
        self.assertEqual(self.obs['not_mixed']['chips'],
                         ['Tag is X', 'Tag is not Y'])

    def test_a_two_element_pair_reads_as_is_and_a_third_element_only_means_not_as_not(self):
        """Every URL and saved filter written before "is not" existed is two elements long,
        and must keep meaning exactly what it meant."""
        self.assertEqual(self.obs['not_restored']['entries'],
                         [['status', 'FAIL'], ['status', 'PASS', 'not'], ['account', '1']])
        self.assertEqual(self.obs['not_restored']['rows'], [])

    def test_operator_presses_notify_the_caller_once_each(self):
        """Eight operator presses, row taps and chip clicks across the scenarios; setFrom()
        is not a change."""
        self.assertEqual(self.obs['not_changes'] - self.obs['changes'], 8)


class SavedIsNotTests(_Saved, unittest.TestCase):

    def test_is_not_is_saved_in_the_three_element_shape(self):
        self.assertEqual(self.s['not_saved']['lastPost']['body']['value'],
                         [{'name': 'No fails', 'filters': [['status', 'FAIL', 'not']],
                           'is_default': False}])
        self.assertEqual(self.s['not_saved']['chip'], 'Saved: No fails')

    def test_flipping_the_operator_marks_the_saved_filter_edited(self):
        """Same key, same value, other operator: a different filter, so the chip must not
        go on claiming the saved one is what is on screen."""
        self.assertEqual(self.s['not_flipped']['chip'], 'Saved: No fails (edited)')

    def test_a_record_saved_before_is_not_existed_loads_as_is(self):
        self.assertEqual(self.s['old_shape']['chips'], ['Status is FAIL'])
        self.assertEqual(self.s['old_shape']['rows'], ['c'])
        self.assertEqual(self.s['old_shape']['chip'], 'Saved: Old')

    def test_a_pair_with_an_unknown_operator_is_dropped_not_guessed_at(self):
        self.assertEqual(self.s['bad_op']['chips'], [])

    def test_a_dropped_excluded_value_is_named_with_its_operator(self):
        kind, msg = self.s['not_dropped']['toasts'][-1]
        self.assertEqual(kind, 'warning')
        self.assertIn('Status is not GONE', msg)
        self.assertEqual(self.s['not_dropped']['chips'], ['Account is Account 2'])


if __name__ == '__main__':
    unittest.main()
