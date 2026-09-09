"""Tier 0 - the pure client-side helpers behind the auto-select-format feature
(static/js/format-plan.js: formatPlanStrategyLabel, formatPlanOptionLabel,
formatPlanTable). Planned 2026-08-06, shipped in dev/changelog/494.

Same technique as tests/test_check_modal_js.py: the helpers are top-level functions in
format-plan.js precisely so this can evaluate the file in node and call them directly.
format-plan.js depends on escHtml (util.js) only at call time, not at definition time, so
the harness stubs a minimal escHtml before evaluating - matching how the real page always
loads util.js first.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN_JS = os.path.join(REPO, 'static', 'js', 'format-plan.js')

_EXPORTS = ('formatPlanOptionLabel, formatPlanTable, FORMAT_STRATEGY_KEYS, '
            'GROUP_FORMAT_STRATEGIES, groupStrategyLabel, groupStrategyHelp, '
            'groupStrategyManagesFormat, formatPlanEntry, formatPlanSummary, '
            'formatPlanPinSelect, formatPlanCounts, formatPlanStatus, fetchFormatPlan')

_HARNESS = f"""
const fs = require('fs');
global.escHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const src = fs.readFileSync(process.argv[1], 'utf8');
const api = new Function(src + '\\nreturn {{{_EXPORTS}}};')();
const {{{_EXPORTS}}} = api;
console.log(JSON.stringify(eval(process.argv[2])));
"""

# One winning bucket (1920x1080 @ 60, 27 channels) and one non-winning bucket, matching
# health check 20's real numbers from the 2026-08-06 planning session - the same fixture
# tests/test_format_selection.py uses server-side.
_PLAN = ('{buckets: ['
         '{key: ["1920x1080", 60], label: "1920x1080 @ 60", resolution: "1920x1080", fps: 60, '
         'pixels: 2073600, channel_ids: [1, 2], count: 27, rank_count: 27, pass_count: 27, '
         'warn_count: 0, median_bitrate_kbps: 3740, median_bpp: 0.03}, '
         '{key: ["1280x720", 60], label: "1280x720 @ 60", resolution: "1280x720", fps: 60, '
         'pixels: 921600, channel_ids: [3], count: 22, rank_count: 22, pass_count: 22, '
         'warn_count: 0, median_bitrate_kbps: null, median_bpp: null}], '
         'strategies: {'
         'highest_bitrate: {key: ["1920x1080", 60], label: "1920x1080 @ 60", resolution: "1920x1080", '
         'fps: 60, channel_ids: [1, 2], count: 27, rank_count: 27, pass_count: 27, warn_count: 0, '
         'rationale: "27 recordable channels at a median 3.74 Mb/s"}, '
         'balanced: {key: null, label: null, resolution: null, fps: null, channel_ids: [], count: 0, '
         'rank_count: 0, pass_count: 0, warn_count: 0, '
         'rationale: "No format has enough healthy channels to build a group on."}'
         '}}')

# The group-5 shape: a big bucket the group records from none of, and a smaller one it
# records from entirely. `rank_total` differs from `total`, which is what puts the
# consequence line onto the recording population.
_NARROWED = ('{total: 105, rank_total: 29, rank_measured: 26, buckets: ['
             '{key: ["1280x720", 30], label: "1280x720 @ 30", resolution: "1280x720", fps: 30, '
             'pixels: 921600, channel_ids: [1], count: 40, rank_count: 0, pass_count: 40, '
             'warn_count: 0, median_bitrate_kbps: 3691, median_bpp: null}, '
             '{key: ["1920x1080", 60], label: "1920x1080 @ 60", resolution: "1920x1080", fps: 60, '
             'pixels: 2073600, channel_ids: [2], count: 26, rank_count: 26, pass_count: 0, '
             'warn_count: 26, median_bitrate_kbps: 3439, median_bpp: null}], '
             'strategies: {'
             'highest_bitrate: {key: ["1920x1080", 60], label: "1920x1080 @ 60", '
             'resolution: "1920x1080", fps: 60, channel_ids: [2], count: 26, rank_count: 26, '
             'pass_count: 0, warn_count: 26, rationale: "26 recordable channels"}'
             '}}')


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
class _Base(unittest.TestCase):
    def evaluate(self, expr):
        proc = subprocess.run(['node', '-e', _HARNESS, PLAN_JS, expr],
                              capture_output=True, text=True, cwd=REPO, timeout=60)
        if proc.returncode != 0:
            self.fail(f'node failed evaluating `{expr}`:\n{proc.stderr}')
        return json.loads(proc.stdout)


class StrategyLabelTests(_Base):
    def test_known_strategy(self):
        self.assertEqual(self.evaluate("groupStrategyLabel('highest_bitrate')"), 'Highest bitrate')
        self.assertEqual(self.evaluate("groupStrategyLabel('balanced')"), 'Balanced')

    def test_all_four_engine_strategies_are_declared(self):
        self.assertEqual(self.evaluate('FORMAT_STRATEGY_KEYS'),
                         ['highest_bitrate', 'highest_resolution', 'most_channels', 'balanced'])

    def test_unknown_strategy_falls_back_to_the_key_itself(self):
        self.assertEqual(self.evaluate("groupStrategyLabel('nonsense')"), 'nonsense')


class OptionLabelTests(_Base):
    def test_winner_carries_format_and_count(self):
        got = self.evaluate(f"formatPlanOptionLabel({_PLAN}, 'highest_bitrate')")
        self.assertEqual(got, 'Highest bitrate - 1920x1080 @ 60 (27 channels)')

    def test_singular_channel_count(self):
        plan = _PLAN.replace('count: 27', 'count: 1')
        got = self.evaluate(f"formatPlanOptionLabel({plan}, 'highest_bitrate')")
        self.assertIn('(1 channel)', got)
        self.assertNotIn('(1 channels)', got)

    def test_no_winner_states_why_rather_than_a_blank_option(self):
        got = self.evaluate(f"formatPlanOptionLabel({_PLAN}, 'balanced')")
        self.assertEqual(got, 'Balanced - no eligible format')

    def test_strategy_missing_from_plan_also_reads_as_no_winner(self):
        got = self.evaluate(f"formatPlanOptionLabel({_PLAN}, 'most_channels')")
        self.assertEqual(got, 'Most members - no eligible format')


class TableTests(_Base):
    def test_empty_plan_renders_nothing(self):
        self.assertEqual(self.evaluate("formatPlanTable({buckets: []}, 'highest_bitrate')"), '')
        self.assertEqual(self.evaluate("formatPlanTable(null, 'highest_bitrate')"), '')

    def test_winning_bucket_is_marked(self):
        html = self.evaluate(f"formatPlanTable({_PLAN}, 'highest_bitrate')")
        self.assertIn('fp-winner', html)
        self.assertIn('pr-picked', html)
        self.assertIn('1920x1080 @ 60', html)

    def test_non_winning_strategy_marks_no_row(self):
        html = self.evaluate(f"formatPlanTable({_PLAN}, 'balanced')")
        self.assertNotIn('fp-winner', html)

    def test_unmeasured_bitrate_renders_as_dash_not_a_wrong_number(self):
        html = self.evaluate(f"formatPlanTable({_PLAN}, 'highest_bitrate')")
        self.assertIn('>--<', html)

    def test_bitrate_formatted_as_mb_per_s_matching_bitratecell(self):
        html = self.evaluate(f"formatPlanTable({_PLAN}, 'highest_bitrate')")
        self.assertIn('3.74 MB/s', html)

    def test_uses_the_shared_table_system(self):
        html = self.evaluate(f"formatPlanTable({_PLAN}, 'highest_bitrate')")
        self.assertIn('table-scroll', html)
        self.assertIn('class="tbl"', html)


# ── dev/changelog/756 - the standing setting's eight values ──────────────────
#
# DESIGN-channel-groups-model.md §4.4. Four of the eight are the bucket-ranking engine the
# tests above cover; the other four are answered outside it, and getting one of them wrong
# means a dropdown option that either 400s or silently enforces the wrong format.


class GroupStrategyListTests(_Base):
    def test_all_eight_values_in_dropdown_order(self):
        keys = [row[0] for row in self.evaluate('GROUP_FORMAT_STRATEGIES')]
        self.assertEqual(keys, [
            'health_check_only', 'highest_score', 'highest_bitrate', 'highest_resolution',
            'most_channels', 'balanced', 'manual', 'unmanaged'])

    def test_the_default_is_first(self):
        """A user looking for "I only want this for a health check" must find it at the top,
        not as an empty spot in the dropdown."""
        self.assertEqual(self.evaluate('GROUP_FORMAT_STRATEGIES')[0][0], 'health_check_only')

    def test_every_value_ships_one_sentence_of_help(self):
        """§4.4: a setting whose owner cannot say what it does is a number the user cannot
        explain, which principle 1 rates worse than no number."""
        for key, label, help_text in self.evaluate('GROUP_FORMAT_STRATEGIES'):
            self.assertTrue(label, key)
            self.assertGreater(len(help_text), 40, key)
            self.assertTrue(help_text.endswith('.'), key)

    def test_balanced_help_names_the_coverage_floor(self):
        """The one whose name does not say what it does. Its help says what the code does."""
        self.assertIn('60%', self.evaluate("groupStrategyHelp('balanced')"))

    def test_only_two_values_manage_no_format(self):
        managed = {k: self.evaluate(f"groupStrategyManagesFormat('{k}')")
                   for k, _, _ in self.evaluate('GROUP_FORMAT_STRATEGIES')}
        self.assertEqual({k for k, v in managed.items() if not v},
                         {'health_check_only', 'unmanaged'})


class NonEngineEntryTests(_Base):
    def test_the_three_formatless_values_say_what_they_do(self):
        """"No eligible format" would be a true sentence about the wrong question for a
        value that never had a bucket to win."""
        self.assertEqual(self.evaluate(f"formatPlanOptionLabel({_PLAN}, 'health_check_only')"),
                         'Health check only - not a recording source')
        self.assertEqual(self.evaluate(f"formatPlanOptionLabel({_PLAN}, 'unmanaged')"),
                         'No format management - any format, mixed')
        self.assertEqual(self.evaluate(f"formatPlanOptionLabel({_PLAN}, 'manual')"),
                         'Pinned format - you pick the format')

    def test_highest_score_follows_the_derived_reference(self):
        got = self.evaluate(
            f"formatPlanOptionLabel({_PLAN}, 'highest_score', '1280x720 @ 60')")
        self.assertEqual(got, "Healthiest member's format - 1280x720 @ 60 (22 channels)")

    def test_the_two_formatless_values_have_no_entry_at_all(self):
        self.assertIsNone(self.evaluate(f"formatPlanEntry({_PLAN}, 'unmanaged')"))
        self.assertIsNone(self.evaluate(f"formatPlanEntry({_PLAN}, 'health_check_only')"))

    def test_the_pending_pin_moves_the_winner_before_it_is_saved(self):
        """Without it the preview answers for the stored value and the marker does not move
        until after you commit, which is the one thing a preview exists to prevent."""
        html = self.evaluate(f"formatPlanTable({_PLAN}, 'manual', '1280x720 @ 60')")
        rows = [r for r in html.split('<tr') if 'fp-winner' in r]
        self.assertEqual(len(rows), 1)
        self.assertIn('1280x720 @ 60', rows[0])


class SummaryTests(_Base):
    def test_it_says_nothing_is_turned_off(self):
        """§4.1's whole correction: the lock filters where members are chosen and never
        mutates a participation switch."""
        got = self.evaluate(f"formatPlanSummary({_PLAN}, 'highest_bitrate', null, null, 30)")
        self.assertIn('Records from 27 of 30 members.', got)
        self.assertIn('Nothing is turned off.', got)

    def test_a_perfect_match_offers_no_consequence_clause(self):
        got = self.evaluate(f"formatPlanSummary({_PLAN}, 'highest_bitrate', null, null, 27)")
        self.assertEqual(got, 'Records from 27 of 27 members.')

    def test_health_check_only_says_nothing_will_record(self):
        got = self.evaluate(f"formatPlanSummary({_PLAN}, 'health_check_only', null, null, 30)")
        self.assertIn('cannot be added to the TV Guide', got)

    def test_unmanaged_names_the_risk_it_accepts(self):
        got = self.evaluate(f"formatPlanSummary({_PLAN}, 'unmanaged', null, null, 30)")
        self.assertIn('all 30 members stay eligible', got)
        self.assertIn('changes partway through', got)

    def test_manual_with_nothing_measured_says_so_rather_than_no_winner(self):
        got = self.evaluate("formatPlanSummary({buckets: []}, 'manual', null, null, 3)")
        self.assertIn('nothing to pin', got)

    def test_an_untested_member_is_not_counted_as_one_that_will_be_skipped(self):
        """Guards dev/docs/BUGS.md 2026-08-19 @ 09:41 PM. An untested member is never
        filtered out - unknown is not proven-different (CLAUDE.md "Format lock filters,
        health score ranks") - so counting the whole remainder as "will be skipped" told
        the user their feeds were being dropped when every one of them was still
        eligible. Measured on the live database: a 24-member group with 2 tested claimed
        22 would be skipped; the true answer is zero."""
        got = self.evaluate(
            f"formatPlanSummary({_PLAN}, 'highest_bitrate', null, null, 30, 27)")
        self.assertIn('Records from 27 of 30 members.', got)
        self.assertIn('never been tested', got)
        self.assertIn('stay eligible', got)
        self.assertNotIn('skipped', got,
                         'none of the 3 non-winners measures a different format - they '
                         'are simply untested, and untested is not skipped')

    def test_a_measured_mismatch_IS_named_as_skipped(self):
        """The other half: a member that really does measure a different format is
        filtered where members are chosen, and the summary has to say so."""
        got = self.evaluate(
            f"formatPlanSummary({_PLAN}, 'highest_bitrate', null, null, 30, 30)")
        self.assertIn('measure a different format', got)
        self.assertIn('skipped', got)
        self.assertNotIn('never been tested', got)

    def test_both_groups_are_named_separately_when_both_exist(self):
        """One number covering two populations that behave differently is the number
        nobody can explain."""
        got = self.evaluate(
            f"formatPlanSummary({_PLAN}, 'highest_bitrate', null, null, 30, 28)")
        self.assertIn('1 measures a different format', got)
        self.assertIn('2 have never been tested', got)

    def test_omitting_the_measured_count_stays_vague_rather_than_guessing(self):
        """A caller that cannot supply the split gets a true sentence, never a made-up
        number - failure paths must be observable, not papered over."""
        got = self.evaluate(f"formatPlanSummary({_PLAN}, 'highest_bitrate', null, null, 30)")
        self.assertIn('only where', got)
        self.assertIn('never been tested stays eligible', got)


class PinSelectTests(_Base):
    def test_offers_every_measured_format(self):
        html = self.evaluate(f"formatPlanPinSelect('p', {_PLAN}, '1920x1080 @ 60')")
        self.assertIn('<option value="1920x1080 @ 60" selected>', html)
        self.assertIn('<option value="1280x720 @ 60">', html)

    def test_a_pin_no_member_measures_any_more_is_still_offered(self):
        """A format stays pinned after the member that justified it changed or left the
        group; dropping it from the list would silently unpin it."""
        html = self.evaluate(f"formatPlanPinSelect('p', {_PLAN}, '3840x2160 @ 60')")
        self.assertIn('<option value="3840x2160 @ 60" selected>', html)

    def test_nothing_measured_disables_the_control_rather_than_offering_a_blank(self):
        html = self.evaluate("formatPlanPinSelect('p', {buckets: []}, null)")
        self.assertIn('disabled', html)
        self.assertIn('No format measured yet', html)


class CountDisclosureTests(_Base):
    """dev/docs/BUGS.md 2026-09-09 07:02, raised against group 7's picker: the
    dropdown offered "Highest bitrate - 3840x2160 @ 50 (5 channels)" where all five were
    warning, and turning Recording off on all five left the same "5 channels" on screen.
    A count the user cannot act on is worth less than none (product principle 1)."""

    def test_a_recording_count_that_matches_the_total_is_not_repeated(self):
        got = self.evaluate(f"formatPlanOptionLabel({_PLAN}, 'highest_bitrate')")
        self.assertEqual('Highest bitrate - 1920x1080 @ 60 (27 channels)', got)

    def test_a_differing_recording_count_is_named(self):
        plan = _PLAN.replace('count: 27, rank_count: 27', 'count: 27, rank_count: 4')
        got = self.evaluate(f"formatPlanOptionLabel({plan}, 'highest_bitrate')")
        self.assertIn('27 channels, 4 recording', got)

    def test_an_all_warning_winner_says_so_in_the_option_itself(self):
        got = self.evaluate(f"formatPlanOptionLabel({_NARROWED}, 'highest_bitrate')")
        self.assertIn('all warning', got)

    def test_a_mixed_bucket_does_not_claim_all_warning(self):
        plan = _PLAN.replace('pass_count: 27, warn_count: 0',
                             'pass_count: 20, warn_count: 7')
        got = self.evaluate(f"formatPlanOptionLabel({plan}, 'highest_bitrate')")
        self.assertNotIn('warning', got)

    def test_counts_render_for_the_two_synthesized_entries_too(self):
        """`manual` and `highest_score` are answered client-side from a bucket lookup, and
        they carry the same fields so one formatter describes all eight values."""
        manual = self.evaluate(
            f"formatPlanEntry({_NARROWED}, 'manual', '1920x1080 @ 60', null)")
        self.assertEqual(26, manual['count'])
        self.assertEqual(0, manual['pass_count'])
        score = self.evaluate(
            f"formatPlanEntry({_NARROWED}, 'highest_score', null, '1280x720 @ 30')")
        self.assertEqual(0, score['rank_count'])

    def test_a_pin_no_bucket_matches_reports_zeros_rather_than_undefined(self):
        entry = self.evaluate(f"formatPlanEntry({_PLAN}, 'manual', '640x480 @ 24', null)")
        self.assertEqual(0, entry['count'])
        self.assertEqual(0, entry['rank_count'])


class StatusColumnTests(_Base):
    """The bucket table has to show the pass/warn split, because a WARN counts toward the
    format decision and the row is the only place that fact can be seen."""

    def test_all_warning_is_called_out(self):
        html = self.evaluate(f"formatPlanTable({_NARROWED}, 'highest_bitrate')")
        self.assertIn('fp-all-warn', html)
        self.assertIn('26 warn', html)

    def test_a_clean_bucket_reads_as_pass(self):
        self.assertEqual('27 pass', self.evaluate(
            "formatPlanStatus({pass_count: 27, warn_count: 0})"))

    def test_a_mixed_bucket_names_both(self):
        self.assertEqual('20 pass, 7 warn', self.evaluate(
            "formatPlanStatus({pass_count: 20, warn_count: 7})"))

    def test_a_bucket_the_group_cannot_record_from_is_dimmed_not_hidden(self):
        """It is the answer to "why did the 40-channel format lose to the 26-channel
        one", so removing it from the table would remove the explanation."""
        html = self.evaluate(f"formatPlanTable({_NARROWED}, 'highest_bitrate')")
        self.assertIn('fp-unrankable', html)
        self.assertIn('1280x720 @ 30', html)
        self.assertIn('data-tip', html)

    def test_recordable_buckets_sort_above_unrecordable_ones(self):
        html = self.evaluate(f"formatPlanTable({_NARROWED}, 'highest_bitrate')")
        self.assertLess(html.index('1920x1080 @ 60'), html.index('1280x720 @ 30'),
                        'the 26-channel candidate must outrank the 40-channel non-candidate')


class NarrowedSummaryTests(_Base):
    """When the server ranked over the recording-enabled members, the consequence line
    describes THAT population - "records from 26 of 105 members" describes a group whose
    other 79 members were never recording sources."""

    def test_the_sentence_counts_members_set_to_record(self):
        got = self.evaluate(f"formatPlanSummary({_NARROWED}, 'highest_bitrate', null, null, 105, 90)")
        self.assertIn('Records from 26 of 29 members set to record.', got)

    def test_the_split_uses_the_servers_measured_count_not_the_callers(self):
        """rank_measured is 26 of 29, so 3 are untested and none measures a different
        format. The caller's own 90-of-105 would have claimed 64 mismatches."""
        got = self.evaluate(f"formatPlanSummary({_NARROWED}, 'highest_bitrate', null, null, 105, 90)")
        self.assertIn('3 have never been tested', got)
        self.assertNotIn('different format', got)

    def test_an_unnarrowed_plan_still_trusts_the_caller(self):
        """The create/clone preview edits a kept set the server has never seen, so its own
        counts must keep winning there."""
        got = self.evaluate(f"formatPlanSummary({_PLAN}, 'highest_bitrate', null, null, 30, 27)")
        self.assertIn('Records from 27 of 30 members.', got)

    def test_no_winner_repeats_the_servers_specific_reason(self):
        """Three situations produce no winner and take three different actions to fix, so
        the client must not flatten them back into one sentence."""
        plan = ('{buckets: [{label: "x", count: 1, rank_count: 0, pass_count: 1, warn_count: 0}], '
                'strategies: {highest_bitrate: {key: null, count: 0, '
                'rationale: "No measured format holds a member this group is set to record from."}}}')
        got = self.evaluate(f"formatPlanSummary({plan}, 'highest_bitrate', null, null, 5, 5)")
        self.assertIn('set to record from', got)


class FetchScopeTests(_Base):
    """dev/docs/BUGS.md 2026-09-09 07:02 - the preview must read what the engine reads, so
    the endpoint is never asked for one job's results."""

    def test_no_job_id_is_ever_sent(self):
        self.assertEqual('/api/channel-groups/7/format-plan',
                         self.evaluate('(() => { let u; global.jsonFetch = (x) => { u = x; }; '
                                       'fetchFormatPlan(7); return u; })()'))
        self.assertEqual('/api/channel-groups/7/format-plan',
                         self.evaluate('(() => { let u; global.jsonFetch = (x) => { u = x; }; '
                                       'fetchFormatPlan(7, {jobId: 3}); return u; })()'))

    def test_the_clone_preview_opts_out_of_the_recording_narrowing(self):
        """A clone's members all start Recording-off, so its own engine ranks over
        everyone and a preview narrowed to the SOURCE group's recording-enabled members
        would describe neither group."""
        self.assertEqual('/api/channel-groups/7/format-plan?rank_scope=all',
                         self.evaluate('(() => { let u; global.jsonFetch = (x) => { u = x; }; '
                                       "fetchFormatPlan(7, {rankScope: 'all'}); return u; })()"))


if __name__ == '__main__':
    unittest.main(verbosity=2)
