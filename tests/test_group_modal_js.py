"""Tier 0 - the shared "Group Channels" modal, driven in a real DOM.

`static/js/group-modal.js` is what the group detail page's "Group with duplicates" and
"+ Add Matching Channels", and the channel search's "adding to a group" context, all open.
Two defects in it are fixed here (`dev/changelog/832`) and neither is reachable from
Python, because both are states the modal passes THROUGH rather than markup a route
renders:

  * It drew the incoming selection as `.badge b-abort` pills - the app's visual language
    for a filter chip you click to remove - on something with no click handler at all, in
    a colour the badge scale reserves for a status. It is a `.cg-picked` list now, the one
    the group-create flow already used, moved into this file because this is the one of
    the two loaded on BOTH pages.
  * "+ Add Matching Channels" opened with an empty body and both footer buttons live while
    `/api/channel-groups/suggest` ran, so it was indistinguishable from a finished modal
    that had found nothing - and clicking Add Channels answered "Select at least one
    channel", blaming the user for the app still working. Worse, the fetch's rejection was
    swallowed entirely, leaving that empty modal up forever with nothing said.

Cancel stays enabled throughout on purpose: nothing has been written while the fetch is
out, so leaving is always safe, and a request that hangs without rejecting would otherwise
leave no usable footer at all.

`tests/support/group_modal.mjs` drives it and reports observations; every assertion lives
here. What it cannot cover: jsdom computes no layout, so where the spinner sits, whether
the picked list scrolls at nine rows, and 375px are all browser work.

  python3 -m unittest tests.test_group_modal_js
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'group_modal.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    """Drive the modal once in node and return every scenario's observations."""
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    proc = subprocess.run([shutil.which('node'), HARNESS, REPO],
                          capture_output=True, text=True, cwd=REPO, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr[-4000:]}')
    _RESULT = json.loads(proc.stdout)
    return _RESULT


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class _Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()

    def test_the_modal_booted_without_an_error(self):
        """Every assertion below is worthless if the script threw - an empty modal is also
        what one whose script died on line one looks like."""
        self.assertEqual(self.obs['errors'], [])


class PickedChannelListTests(_Base):
    """The selection is a list of channels, not a row of chips."""

    def test_the_selection_renders_as_list_rows(self):
        frame = self.obs['selection_only']['open']
        self.assertEqual(frame['picked_rows'], ['Fox Sports 1', '<b>FS1</b> HD'])

    def test_no_badge_pretends_the_selection_is_removable(self):
        """`.badge b-abort` advertised a click this modal has never had. The check is for
        the badge class anywhere in the modal, not merely for that one string in the
        source, so restyling it back under another name fails here too."""
        for name in ('selection_only', 'fixed_with_selection', 'single_seed'):
            with self.subTest(scenario=name):
                self.assertEqual(self.obs[name]['open']['abort_badges'], 0)
                self.assertEqual(self.obs[name]['open']['badges'], 0)

    def test_the_channel_name_is_escaped(self):
        """Names come from provider data. `<b>FS1</b> HD` surviving as text rather than as
        markup is what says escHtml is still in the path."""
        self.assertIn('<b>FS1</b> HD', self.obs['selection_only']['open']['picked_rows'])

    def test_the_list_is_labelled_with_its_own_count(self):
        self.assertEqual(self.obs['selection_only']['open']['picked_label'],
                         'Channels selected (2)')

    def test_a_channel_with_an_account_gets_its_dot(self):
        """Which provider a feed came from is what tells two same-named copies apart, so
        the caller's account colour rides along into the list."""
        self.assertEqual(self.obs['selection_only']['open']['picked_dots'], 1)
        self.assertEqual(self.obs['fixed_with_selection']['open']['picked_dots'], 1)


class SuggestLoadingStateTests(_Base):
    """"+ Add Matching Channels" while the suggest fetch is out."""

    def test_the_modal_says_it_is_working(self):
        frame = self.obs['fixed_results']['open']
        self.assertTrue(frame['loading'])
        self.assertIn('Looking for channels to add', frame['loading_text'])
        self.assertFalse(frame['suggest_shown'])

    def test_add_channels_is_disabled_while_the_request_is_out(self):
        """There is nothing to send yet, so the only thing the click could produce is
        "Select at least one channel"."""
        self.assertIs(self.obs['fixed_results']['open']['submit_disabled'], True)

    def test_cancel_stays_live_while_the_request_is_out(self):
        """Nothing has been written, so leaving is always safe - and a fetch that hangs
        without rejecting must never leave the modal with no usable footer."""
        for name in ('fixed_results', 'fixed_empty', 'fixed_error'):
            with self.subTest(scenario=name):
                self.assertIs(self.obs[name]['open']['cancel_disabled'], False)

    def test_results_clear_the_loading_row_and_give_the_button_back(self):
        frame = self.obs['fixed_results']['settled']
        self.assertFalse(frame['loading'])
        self.assertTrue(frame['suggest_shown'])
        self.assertEqual(frame['suggest_rows'], 2)
        self.assertIs(frame['submit_disabled'], False)


class SuggestSettleTests(_Base):
    """The two endings that used to leave an empty modal with nothing said."""

    def test_an_empty_result_says_so(self):
        frame = self.obs['fixed_empty']['settled']
        self.assertFalse(frame['loading'])
        self.assertTrue(frame['empty_shown'])
        self.assertIn('Fox Sports 1', frame['empty_text'])

    def test_an_empty_result_with_nothing_selected_leaves_the_button_disabled(self):
        """Re-enabling it here would hand back the same unanswerable click: this modal
        holds no selection and no suggestions, so there is nothing for a submit to send.
        The message beside it is the honest answer."""
        self.assertIs(self.obs['fixed_empty']['settled']['submit_disabled'], True)

    def test_an_empty_result_with_a_selection_gives_the_button_back(self):
        """The channel search's "adding to a group" context arrives with channels already
        picked, so no suggestions is not the same as nothing to do."""
        frame = self.obs['fixed_with_selection']['settled']
        self.assertIs(frame['submit_disabled'], False)
        self.assertEqual(frame['picked_rows'], ['FS1 East'])

    def test_a_failed_request_clears_the_spinner_and_names_the_failure(self):
        """A spinner that never comes down is worse than no spinner. The rejection was
        swallowed outright before this."""
        frame = self.obs['fixed_error']['settled']
        self.assertFalse(frame['loading'])
        self.assertTrue(frame['error_shown'])
        self.assertIn('Service Unavailable', frame['error_text'])


class SuggestProviderRemovedTests(_Base):
    """A suggestion the provider has already dropped carries the same Missing badge the
    group's member table and the Remove Duplicates modal show (dev/docs/BUGS.md 2026-09-19
    @ 02:38:01 PM). Marked, not filtered: it is still a row and still checkable."""

    def test_the_removed_candidate_is_badged_and_the_live_one_is_not(self):
        rows = {r['name']: r for r in self.obs['fixed_missing']['settled']['suggest_badges']}
        self.assertIsNone(rows['Fox Sports 1 HD']['badge'])
        self.assertEqual(rows['FS1 backup']['badge'], 'Missing 2026-09-01')
        self.assertIn("No longer seen in Acct B's synced feed since 2026-09-01",
                      rows['FS1 backup']['tip'])

    def test_the_removed_candidate_is_still_offered(self):
        rows = {r['name']: r for r in self.obs['fixed_missing']['settled']['suggest_badges']}
        self.assertTrue(rows['FS1 backup']['checkable'])


class SingleSeedPathUnchangedTests(_Base):
    """The group detail page's per-row "Group with duplicates" seeds suggestions too, but
    it never opened on an empty body - it has its selection and its name field from the
    first frame - so it is deliberately untouched by the loading state."""

    def test_it_shows_no_loading_row_and_never_disables_its_button(self):
        for label in ('open', 'settled'):
            with self.subTest(frame=label):
                frame = self.obs['single_seed'][label]
                self.assertFalse(frame['loading'])
                self.assertIs(frame['submit_disabled'], False)

    def test_an_empty_result_is_not_announced_there(self):
        """"We found nothing extra" is not news on a modal that already holds what you
        picked; the suggestions are an offer on top of it."""
        self.assertFalse(self.obs['single_seed']['settled']['empty_shown'])


if __name__ == '__main__':
    unittest.main()
