"""The channel hide rule engine: GLOB patterns over category and name, global or per account.

Sources 1, 2 and 3 of the four that stack into `Channel.hidden` (app/channel_hiding.py,
dev/docs/DESIGN-channel-hiding.md, dev/changelog/776). Three classes here are load-bearing
rather than incidental:

- `GlobSemanticsTests` pins what a pattern actually means. `A?`, `A*`, `*A*`, `*A` and `A*Z`
  are the five shapes the feature was specified in, and they are asserted end to end through
  a real rule rather than against a matcher, because the thing a user is promised is what the
  rule hides - not what a helper returns.
- `PreviewAgreesWithTheMaterializerTests` is what keeps the two spellings of "what does this
  rule match" honest. The materializer resolves a category GLOB against the 1,719-value
  category list once per pass; the preview asks it per channel row, because an unsaved
  pattern has no resolved set to consult. They are the same question and must never disagree
  - a preview that undercounts is exactly how somebody hides 13,920 channels by accident.
- `RuleAdmissionTests` covers the refusal path. A refused pass leaves rules saved but not
  applied, which is invisible by construction: nothing errors, the channel is simply still
  offered. The refusal has to reach a surface.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import admission, channel_hiding, db  # noqa: E402
from app.database import (  # noqa: E402
    Account, Channel, ChannelHideRule, HIDE_TARGET_CATEGORY_EXACT,
    HIDE_TARGET_CATEGORY_GLOB, HIDE_TARGET_NAME_GLOB)


class _RuleTestCase(unittest.TestCase):

    #: Subclasses that assert on queued retry jobs need the real jobstore - without a
    #: scheduler a deferral has nowhere to queue, so there is no pending state to report.
    START_SCHEDULER = False

    def setUp(self):
        self.t = make_test_app(start_scheduler=self.START_SCHEDULER)
        # CSRF is app-wide and a token is the part a browser supplies; tests/test_csrf.py
        # owns the protection itself.
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.ctx_mgr = self.t.app.app_context()
        self.ctx_mgr.push()
        self.acct = seed.make_account(name='Alpha')

    def tearDown(self):
        self.ctx_mgr.pop()
        self.t.cleanup()

    def _channel(self, name, category=None, account=None, **kw):
        return seed.make_channel(account or self.acct, name=name,
                                 category_name=category, **kw)

    def _rule(self, target, pattern, account_id=None, enabled=True):
        rule = ChannelHideRule(target=target, pattern=pattern, account_id=account_id,
                               enabled=enabled)
        db.session.add(rule)
        db.session.flush()
        return rule

    def _apply(self):
        """Recompute the whole table and commit, the way a rule save does."""
        channel_hiding.recompute()
        db.session.commit()
        db.session.expire_all()

    def _hidden_names(self):
        return sorted(c.name for c in Channel.query.filter(Channel.hidden.is_(True)))

    def _post(self, path, payload):
        return self.t.app.test_client().post(path, json=payload)


# ---------------------------------------------------------------------------


class GlobSemanticsTests(_RuleTestCase):
    """What a pattern means, asserted through a real rule rather than a matcher.

    SQLite GLOB is a WHOLE-STRING match with `*`, `?` and `[...]`, and is natively
    case-sensitive - which is what makes `*AR*` catch `HALLMARK HD` and `PARAMOUNT HD` while
    leaving `hallmark hd` alone. Verified against SQLite itself before the engine was
    written; pinned here so nobody re-derives it from a regex intuition.
    """

    NAMES = ['A', 'AB', 'ABC', 'AZ', 'ABZ', 'AZZ', 'BA', 'BAB', 'ZA', 'aB', 'BB']

    def _hidden_by(self, pattern):
        for name in self.NAMES:
            self._channel(name)
        self._rule(HIDE_TARGET_NAME_GLOB, pattern)
        self._apply()
        return self._hidden_names()

    def test_question_mark_is_exactly_one_more_character(self):
        # Not bare 'A' (nothing follows) and not 'ABC' (two follow).
        self.assertEqual(self._hidden_by('A?'), ['AB', 'AZ'])

    def test_star_is_any_number_of_characters_including_none(self):
        self.assertEqual(self._hidden_by('A*'), ['A', 'AB', 'ABC', 'ABZ', 'AZ', 'AZZ'])

    def test_star_both_ends_is_contains(self):
        self.assertEqual(self._hidden_by('*A*'),
                         ['A', 'AB', 'ABC', 'ABZ', 'AZ', 'AZZ', 'BA', 'BAB', 'ZA'])

    def test_leading_star_is_ends_with(self):
        self.assertEqual(self._hidden_by('*A'), ['A', 'BA', 'ZA'])

    def test_a_star_between_two_literals_anchors_both_ends(self):
        self.assertEqual(self._hidden_by('A*Z'), ['ABZ', 'AZ', 'AZZ'])

    def test_matching_is_case_sensitive(self):
        # 'aB' is in NAMES and is absent from every result above; asserted directly so the
        # property is stated rather than implied by an omission.
        self.assertNotIn('aB', self._hidden_by('*A*'))

    def test_a_pattern_that_matches_nothing_hides_nothing(self):
        self.assertEqual(self._hidden_by('Q*'), [])


class RuleSourceTests(_RuleTestCase):
    """Each of the three rule sources, and what lands in `hidden_reason`."""

    def setUp(self):
        super().setUp()
        self.news = self._channel('BBC News', category='UK| News')
        self.sport = self._channel('Sky Sports', category='UK| Sports')
        self.movie = self._channel('AR| Cinema', category='AR| Movies')

    def test_a_category_glob_hides_by_category_not_by_name(self):
        self._rule(HIDE_TARGET_CATEGORY_GLOB, 'UK| *')
        self._apply()
        self.assertEqual(self._hidden_names(), ['BBC News', 'Sky Sports'])
        self.assertEqual(db.session.get(Channel, self.news.id).hidden_reason,
                         channel_hiding.HIDE_REASON_CATEGORY_GLOB)

    def test_a_category_glob_catches_a_channel_whose_own_name_has_no_prefix(self):
        """The measured reason categories are the better axis: on the live database the
        category rules catch 18,925 channels the name rules miss - an Arabic-titled channel
        in an `AR|` category whose own name carries no prefix."""
        plain = self._channel('Al Jazeera', category='AR| News')
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        self._apply()
        self.assertNotIn('Al Jazeera', self._hidden_names())

        self._rule(HIDE_TARGET_CATEGORY_GLOB, 'AR| *')
        self._apply()
        self.assertIn('Al Jazeera', self._hidden_names())
        self.assertEqual(db.session.get(Channel, plain.id).hidden_reason,
                         channel_hiding.HIDE_REASON_CATEGORY_GLOB)

    def test_an_exact_category_pick_hides_only_that_category(self):
        self._rule(HIDE_TARGET_CATEGORY_EXACT, 'UK| Sports')
        self._apply()
        self.assertEqual(self._hidden_names(), ['Sky Sports'])
        self.assertEqual(db.session.get(Channel, self.sport.id).hidden_reason,
                         channel_hiding.HIDE_REASON_CATEGORY_EXACT)

    def test_a_name_glob_hides_by_name(self):
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        self._apply()
        self.assertEqual(self._hidden_names(), ['AR| Cinema'])
        self.assertEqual(db.session.get(Channel, self.movie.id).hidden_reason,
                         channel_hiding.HIDE_REASON_NAME_GLOB)

    def test_a_disabled_rule_hides_nothing(self):
        self._rule(HIDE_TARGET_NAME_GLOB, '*', enabled=False)
        self._apply()
        self.assertEqual(self._hidden_names(), [])

    def test_a_channel_with_no_category_is_untouched_by_a_category_rule(self):
        loose = self._channel('Uncategorized')
        self._rule(HIDE_TARGET_CATEGORY_GLOB, '*')
        self._apply()
        self.assertNotIn(loose.name, self._hidden_names())

    def test_the_reason_names_the_first_source_in_resolution_order(self):
        """Order is 1 (category glob), 2 (category exact), 3 (name glob). Two sources
        matching the same channel is not a conflict - they agree - so the only thing order
        decides is which name gets displayed."""
        self._rule(HIDE_TARGET_NAME_GLOB, 'BBC*')
        self._rule(HIDE_TARGET_CATEGORY_GLOB, 'UK| N*')
        self._apply()
        self.assertEqual(db.session.get(Channel, self.news.id).hidden_reason,
                         channel_hiding.HIDE_REASON_CATEGORY_GLOB)


class RuleScopeTests(_RuleTestCase):
    """Global versus per-account. One nullable column, not two lists."""

    def setUp(self):
        super().setUp()
        self.other = seed.make_account(name='Beta')
        self.mine = self._channel('AR| One', category='AR| News')
        self.theirs = self._channel('AR| Two', category='AR| News', account=self.other)

    def test_a_global_rule_reaches_every_account(self):
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        self._apply()
        self.assertEqual(self._hidden_names(), ['AR| One', 'AR| Two'])

    def test_an_account_scoped_name_rule_stops_at_its_account(self):
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*', account_id=self.acct.id)
        self._apply()
        self.assertEqual(self._hidden_names(), ['AR| One'])

    def test_an_account_scoped_category_rule_stops_at_its_account(self):
        self._rule(HIDE_TARGET_CATEGORY_GLOB, 'AR| *', account_id=self.other.id)
        self._apply()
        self.assertEqual(self._hidden_names(), ['AR| Two'])

    def test_an_account_scoped_exact_pick_stops_at_its_account(self):
        self._rule(HIDE_TARGET_CATEGORY_EXACT, 'AR| News', account_id=self.acct.id)
        self._apply()
        self.assertEqual(self._hidden_names(), ['AR| One'])


class OverrideBeatsRulesTests(_RuleTestCase):
    """Source 4 wins in BOTH directions - it can force-hide a channel no rule matches and
    force-show one that several do. That is the whole reason it is a tri-state column rather
    than a boolean."""

    def setUp(self):
        super().setUp()
        self.matched = self._channel('AR| One', category='AR| News')
        self.clean = self._channel('BBC News', category='UK| News')
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')

    def test_force_show_survives_a_matching_rule(self):
        channel_hiding.set_hidden_override(self.matched, False)
        self._apply()
        self.assertEqual(self._hidden_names(), [])

    def test_a_force_shown_channel_carries_no_reason(self):
        """A reason on a visible channel is a number nobody can explain - the page would say
        "hidden by a category rule" about a row sitting in plain sight."""
        channel_hiding.set_hidden_override(self.matched, False)
        self._apply()
        self.assertIsNone(db.session.get(Channel, self.matched.id).hidden_reason)

    def test_force_hide_works_where_no_rule_matches(self):
        channel_hiding.set_hidden_override(self.clean, True)
        self._apply()
        self.assertEqual(self._hidden_names(), ['AR| One', 'BBC News'])
        self.assertEqual(db.session.get(Channel, self.clean.id).hidden_reason,
                         channel_hiding.HIDE_REASON_MANUAL)

    def test_clearing_the_override_hands_the_channel_back_to_the_rules(self):
        channel_hiding.set_hidden_override(self.matched, False)
        self._apply()
        self.assertEqual(self._hidden_names(), [])
        channel_hiding.set_hidden_override(self.matched, None)
        self._apply()
        self.assertEqual(self._hidden_names(), ['AR| One'])

    def test_deleting_the_rule_gives_the_channel_back_with_no_re_enable_step(self):
        self._apply()
        self.assertEqual(self._hidden_names(), ['AR| One'])
        ChannelHideRule.query.delete()
        self._apply()
        self.assertEqual(self._hidden_names(), [])


class RuleDeferralTests(_RuleTestCase):
    """Guide/group membership DEFERS a rule hide exactly as it defers a hand hide. Uniform
    across all four sources on purpose: a refusal would make the user remove the channel from
    the guide, come back and re-save the rule."""

    def test_a_guide_row_defers_a_rule_hide(self):
        ch = self._channel('AR| One', in_guide=True)
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        self._apply()
        fresh = db.session.get(Channel, ch.id)
        self.assertFalse(fresh.hidden)
        self.assertTrue(fresh.hidden_deferred)
        self.assertEqual(fresh.hidden_reason, channel_hiding.HIDE_REASON_NAME_GLOB)

    def test_a_group_membership_defers_a_rule_hide(self):
        ch = self._channel('AR| One')
        seed.make_group(name='Keepers', members=[ch], in_guide=False)
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        self._apply()
        fresh = db.session.get(Channel, ch.id)
        self.assertFalse(fresh.hidden)
        self.assertTrue(fresh.hidden_deferred)

    def test_a_rule_hides_the_rest_of_the_batch_around_a_protected_channel(self):
        """The bulk property, at the rule scale: 100 matched channels of which 1 is
        protected hides 99 and reports the 1, rather than the batch failing over one row."""
        protected = self._channel('AR| Protected', in_guide=True)
        for i in range(5):
            self._channel(f'AR| Ordinary {i}')
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        self._apply()
        self.assertEqual(len(self._hidden_names()), 5)
        self.assertTrue(db.session.get(Channel, protected.id).hidden_deferred)

    def test_losing_the_protection_lets_the_rule_take_effect(self):
        ch = self._channel('AR| One', in_guide=True)
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        self._apply()
        self.assertFalse(db.session.get(Channel, ch.id).hidden)
        db.session.get(Channel, ch.id).in_guide = False  # hidden-recompute-ok: the next line
        self._apply()
        self.assertTrue(db.session.get(Channel, ch.id).hidden)


class ResolutionScopeTests(_RuleTestCase):
    """A scoped recompute answers for its scope and leaves everything else alone. A scope
    that leaked would show up as rows silently un-hiding, with nothing to notice it."""

    def setUp(self):
        super().setUp()
        self.other = seed.make_account(name='Beta')
        self.mine = self._channel('AR| One', category='AR| News')
        self.theirs = self._channel('AR| Two', category='AR| News', account=self.other)
        self._rule(HIDE_TARGET_CATEGORY_GLOB, 'AR| *')
        self._apply()

    def test_an_account_scoped_pass_does_not_touch_another_account(self):
        ChannelHideRule.query.delete()
        channel_hiding.recompute(account_id=self.acct.id)
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(self._hidden_names(), ['AR| Two'])

    def test_an_id_scoped_pass_does_not_touch_the_rest(self):
        ChannelHideRule.query.delete()
        channel_hiding.recompute([self.mine.id])
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(self._hidden_names(), ['AR| Two'])

    def test_a_scoped_pass_still_resolves_categories_inside_its_scope(self):
        """The category resolution is narrowed with the same WHERE the UPDATE gets, so a
        scoped pass must not come back with an empty category set and un-hide its own rows."""
        channel_hiding.recompute([self.mine.id])
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(self._hidden_names(), ['AR| One', 'AR| Two'])


class PreviewAgreesWithTheMaterializerTests(_RuleTestCase):
    """The two spellings of "what does this rule match" must give the same answer.

    `resolve_rules()` resolves a category GLOB against the category list once per pass;
    `match_predicate()` asks per channel row, because an unsaved pattern has no resolved set.
    They are the same question and are spelled twice only for cost.
    """

    PATTERNS = [
        (HIDE_TARGET_NAME_GLOB, '*A*'),
        (HIDE_TARGET_NAME_GLOB, 'AR|*'),
        (HIDE_TARGET_NAME_GLOB, 'A?'),
        (HIDE_TARGET_CATEGORY_GLOB, 'AR| *'),
        (HIDE_TARGET_CATEGORY_GLOB, '*News*'),
        (HIDE_TARGET_CATEGORY_EXACT, 'UK| News'),
    ]

    def setUp(self):
        super().setUp()
        for name, category in [('AR| One', 'AR| News'), ('AB', 'UK| News'),
                               ('HALLMARK HD', 'US| Movies'), ('hallmark hd', 'US| Movies'),
                               ('AZ', 'AR| Sports'), ('BBC', 'UK| News')]:
            self._channel(name, category=category)
        db.session.commit()

    def test_every_pattern_hides_exactly_what_the_preview_promised(self):
        for target, pattern in self.PATTERNS:
            with self.subTest(target=target, pattern=pattern):
                promised = channel_hiding.preview(target, pattern)
                ChannelHideRule.query.delete()
                self._rule(target, pattern)
                self._apply()
                self.assertEqual(len(self._hidden_names()), promised['matched'])

    def test_the_sample_names_are_channels_the_rule_really_hides(self):
        promised = channel_hiding.preview(HIDE_TARGET_NAME_GLOB, '*A*')
        self._rule(HIDE_TARGET_NAME_GLOB, '*A*')
        self._apply()
        hidden = set(self._hidden_names())
        self.assertTrue(promised['sample'])
        for row in promised['sample']:
            self.assertIn(row['name'], hidden)

    def test_a_case_sensitive_pattern_is_previewed_case_sensitively(self):
        promised = channel_hiding.preview(HIDE_TARGET_NAME_GLOB, '*HALLMARK*')
        self.assertEqual([r['name'] for r in promised['sample']], ['HALLMARK HD'])

    def test_the_preview_reports_what_protection_would_keep_visible(self):
        ch = Channel.query.filter_by(name='AR| One').one()
        ch.in_guide = True  # hidden-recompute-ok: nothing is hidden yet in this test
        db.session.commit()
        promised = channel_hiding.preview(HIDE_TARGET_NAME_GLOB, 'AR|*')
        self.assertEqual(promised['deferred'], 1)


class RuleStatsTests(_RuleTestCase):
    """The per-rule display cache. Never the authority for what is hidden - `Channel.hidden`
    is - and a rule's count is what its own pattern matches, independently of every other
    rule, because that is the question somebody reading one line is asking."""

    def setUp(self):
        super().setUp()
        for name, category in [('AR| One', 'AR| News'), ('AR| Two', 'AR| News'),
                               ('BBC', 'UK| News')]:
            self._channel(name, category=category)

    def test_a_name_rule_counts_what_it_matches(self):
        rule = self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        channel_hiding.refresh_rule_stats()
        db.session.commit()
        self.assertEqual(rule.match_count, 2)
        self.assertEqual(rule.deferred_count, 0)
        self.assertIsNotNone(rule.counted_at)

    def test_a_category_rule_counts_what_it_matches(self):
        rule = self._rule(HIDE_TARGET_CATEGORY_GLOB, 'AR| *')
        channel_hiding.refresh_rule_stats()
        db.session.commit()
        self.assertEqual(rule.match_count, 2)

    def test_an_exact_pick_counts_its_own_category(self):
        rule = self._rule(HIDE_TARGET_CATEGORY_EXACT, 'UK| News')
        channel_hiding.refresh_rule_stats()
        db.session.commit()
        self.assertEqual(rule.match_count, 1)

    def test_two_rules_matching_the_same_channel_each_count_it(self):
        """Counts are per rule and do not partition the channels between them: "what does
        this line do" is not the same question as "what is hidden"."""
        a = self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        b = self._rule(HIDE_TARGET_CATEGORY_GLOB, 'AR| *')
        channel_hiding.refresh_rule_stats()
        db.session.commit()
        self.assertEqual((a.match_count, b.match_count), (2, 2))

    def test_a_protected_match_is_counted_as_deferred(self):
        Channel.query.filter_by(name='AR| One').one().in_guide = True  # hidden-recompute-ok: counted, not hidden
        rule = self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        channel_hiding.refresh_rule_stats()
        db.session.commit()
        self.assertEqual((rule.match_count, rule.deferred_count), (2, 1))

    def test_a_disabled_rule_is_still_counted(self):
        rule = self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*', enabled=False)
        channel_hiding.refresh_rule_stats()
        db.session.commit()
        self.assertEqual(rule.match_count, 2)

    def test_a_scoped_rule_counts_only_its_own_account(self):
        other = seed.make_account(name='Beta')
        self._channel('AR| Three', category='AR| News', account=other)
        rule = self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*', account_id=self.acct.id)
        channel_hiding.refresh_rule_stats()
        db.session.commit()
        self.assertEqual(rule.match_count, 2)


class HiddenChannelCountTests(_RuleTestCase):
    """Account.hidden_channel_count - refreshed from inside recompute() itself, never
    separately, so it can never disagree with Channel.hidden (dev/docs/DESIGN-channel-hiding.md
    §11 "Counts")."""

    def test_a_whole_table_recompute_sets_every_touched_accounts_count(self):
        self._channel('AR| One')
        self._channel('BBC')
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        self._apply()
        self.assertEqual(db.session.get(Account, self.acct.id).hidden_channel_count, 1)

    def test_a_channel_scoped_recompute_refreshes_just_its_own_account(self):
        other = seed.make_account(name='Beta')
        ch = self._channel('AR| One')
        self._channel('AR| Other', account=other)
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        db.session.commit()
        channel_hiding.recompute([ch.id])
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(db.session.get(Account, self.acct.id).hidden_channel_count, 1)
        # The rule matches "AR| Other" too, but this pass was scoped to the first channel's
        # id only - the other account's count must stay untouched by a scope it wasn't part of.
        self.assertEqual(db.session.get(Account, other.id).hidden_channel_count, 0)

    def test_channel_ids_spanning_two_accounts_refreshes_both(self):
        """The shape accounts.py's duplicate-channel repoint uses: source and dest can
        belong to different accounts, and both need their count refreshed together."""
        other = seed.make_account(name='Beta')
        ch_a = self._channel('AR| One')
        ch_b = self._channel('AR| Two', account=other)
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        db.session.commit()
        channel_hiding.recompute([ch_a.id, ch_b.id])
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(db.session.get(Account, self.acct.id).hidden_channel_count, 1)
        self.assertEqual(db.session.get(Account, other.id).hidden_channel_count, 1)

    def test_un_hiding_drops_the_count_back_down(self):
        ch = self._channel('AR| One')
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        self._apply()
        self.assertEqual(db.session.get(Account, self.acct.id).hidden_channel_count, 1)

        channel_hiding.set_hidden_override(ch, False)
        channel_hiding.recompute([ch.id])
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(db.session.get(Account, self.acct.id).hidden_channel_count, 0)

    def test_an_account_scoped_recompute_does_not_touch_a_sibling_account(self):
        other = seed.make_account(name='Beta')
        self._channel('AR| One')
        self._channel('AR| Two', account=other)
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        db.session.commit()
        channel_hiding.recompute(account_id=self.acct.id)
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(db.session.get(Account, self.acct.id).hidden_channel_count, 1)
        self.assertEqual(db.session.get(Account, other.id).hidden_channel_count, 0)


class RuleAdmissionTests(_RuleTestCase):
    """The materializer is bulk work over the whole channel table, so it takes a ticket
    rather than checking whether anything else is running and then starting."""

    def tearDown(self):
        admission.reset_for_tests()
        super().tearDown()

    def test_it_yields_to_a_sync(self):
        self._channel('AR| One')
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        db.session.commit()
        admission.try_start(admission.KIND_SYNC, 'account 1')
        result = channel_hiding.materialize('rule added')
        self.assertFalse(result.granted)
        self.assertIn('account sync', result.reason)
        self.assertEqual(self._hidden_names(), [])

    def test_force_gets_through_for_the_tail_of_an_admitted_run(self):
        self._channel('AR| One')
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        db.session.commit()
        admission.try_start(admission.KIND_SYNC, 'account 1')
        result = channel_hiding.materialize('sync of Alpha', force=True)
        self.assertTrue(result.granted)
        db.session.expire_all()
        self.assertEqual(self._hidden_names(), ['AR| One'])

    def test_it_does_not_yield_to_a_test_run(self):
        """Health checks run for hours. Yielding to one would starve every rule edit made
        while it is going."""
        admission.try_start(admission.KIND_TESTER, 'nightly')
        self.assertTrue(channel_hiding.materialize('rule added').granted)

    def test_the_ticket_is_released_even_when_the_pass_raises(self):
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        db.session.commit()
        original = channel_hiding.recompute
        channel_hiding.recompute = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom'))
        try:
            with self.assertRaises(RuntimeError):
                channel_hiding.materialize('rule added')
        finally:
            channel_hiding.recompute = original
        self.assertNotIn(admission.KIND_HIDING, admission.active_kinds())

    def test_a_small_scoped_recompute_takes_no_ticket(self):
        """A guide toggle runs inside a request and must never be refused - it is the tiny
        half, and the ticket is for the whole-table pass."""
        ch = self._channel('AR| One')
        self._rule(HIDE_TARGET_NAME_GLOB, 'AR|*')
        admission.try_start(admission.KIND_SYNC, 'account 1')
        channel_hiding.recompute([ch.id])
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(self._hidden_names(), ['AR| One'])


class RuleApiTests(_RuleTestCase):
    """The endpoints. No UI ships with this chunk on purpose."""

    BASE = '/api/channel-hide-rules'

    def setUp(self):
        super().setUp()
        self.client = self.t.app.test_client()
        for name, category in [('AR| One', 'AR| News'), ('AR| Two', 'AR| News'),
                               ('BBC', 'UK| News')]:
            self._channel(name, category=category)
        db.session.commit()

    def _create(self, **payload):
        payload.setdefault('target', HIDE_TARGET_NAME_GLOB)
        return self.client.post(self.BASE, json=payload)

    def test_creating_a_rule_hides_what_it_matches(self):
        resp = self._create(pattern='AR|*')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        self.assertTrue(resp.get_json()['materialized'])
        db.session.expire_all()
        self.assertEqual(self._hidden_names(), ['AR| One', 'AR| Two'])

    def test_the_response_carries_the_preview_that_justified_it(self):
        body = self._create(pattern='AR|*').get_json()
        self.assertEqual(body['preview']['matched'], 2)
        self.assertEqual(len(body['preview']['sample']), 2)

    def test_deleting_a_rule_gives_the_channels_back(self):
        rule_id = self._create(pattern='AR|*').get_json()['rule']['id']
        resp = self.client.delete(f'{self.BASE}/{rule_id}')
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        self.assertEqual(self._hidden_names(), [])

    def test_switching_a_rule_off_gives_the_channels_back(self):
        rule_id = self._create(pattern='AR|*').get_json()['rule']['id']
        resp = self.client.patch(f'{self.BASE}/{rule_id}', json={'enabled': False})
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        self.assertEqual(self._hidden_names(), [])

    def test_editing_a_pattern_re_applies_it(self):
        rule_id = self._create(pattern='AR|*').get_json()['rule']['id']
        self.client.patch(f'{self.BASE}/{rule_id}', json={'pattern': 'BBC*'})
        db.session.expire_all()
        self.assertEqual(self._hidden_names(), ['BBC'])

    def test_an_empty_pattern_is_a_400(self):
        resp = self._create(pattern='')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('empty', resp.get_json()['error'])

    def test_an_unknown_target_is_a_400(self):
        resp = self._create(target='vibes', pattern='AR|*')
        self.assertEqual(resp.status_code, 400)

    def test_an_unknown_account_is_a_400(self):
        resp = self._create(pattern='AR|*', account_id=9999)
        self.assertEqual(resp.status_code, 400)

    def test_a_duplicate_rule_is_a_409_naming_the_existing_one(self):
        first = self._create(pattern='AR|*').get_json()['rule']['id']
        resp = self._create(pattern='AR|*')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()['rule']['id'], first)

    def test_the_same_pattern_at_a_different_scope_is_not_a_duplicate(self):
        self._create(pattern='AR|*')
        resp = self._create(pattern='AR|*', account_id=self.acct.id)
        self.assertEqual(resp.status_code, 200)

    def test_a_rule_that_hides_everything_is_refused_until_confirmed(self):
        resp = self._create(pattern='*')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('every channel', resp.get_json()['error'])
        self.assertEqual(ChannelHideRule.query.count(), 0)

    def test_hides_everything_is_measured_not_sniffed_from_punctuation(self):
        """`[A-Z]*` has no `*`-only shape a syntax check would flag, and matches every
        channel here all the same."""
        resp = self._create(pattern='[A-Z]*')
        self.assertEqual(resp.status_code, 409)

    def test_confirming_gets_the_total_rule_through(self):
        resp = self._create(pattern='*', confirm=True)
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        self.assertEqual(len(self._hidden_names()), 3)

    def test_the_preview_endpoint_saves_nothing(self):
        resp = self.client.post(f'{self.BASE}/preview',
                                json={'target': HIDE_TARGET_NAME_GLOB, 'pattern': 'AR|*'})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body['matched'], 2)
        self.assertEqual(body['scope_total'], 3)
        self.assertFalse(body['hides_everything'])
        self.assertEqual(ChannelHideRule.query.count(), 0)
        db.session.expire_all()
        self.assertEqual(self._hidden_names(), [])

    def test_the_preview_names_the_categories_a_category_pattern_caught(self):
        resp = self.client.post(f'{self.BASE}/preview',
                                json={'target': HIDE_TARGET_CATEGORY_GLOB, 'pattern': '*News*'})
        self.assertEqual(sorted(resp.get_json()['categories']), ['AR| News', 'UK| News'])

    def test_the_category_list_carries_counts(self):
        resp = self.client.get(f'{self.BASE}/categories')
        self.assertEqual(resp.status_code, 200)
        cats = {c['category_name']: c['channel_count'] for c in resp.get_json()['categories']}
        self.assertEqual(cats, {'AR| News': 2, 'UK| News': 1})

    def test_listing_returns_the_rules_with_their_counts(self):
        self._create(pattern='AR|*')
        body = self.client.get(self.BASE).get_json()
        self.assertEqual(len(body['rules']), 1)
        self.assertEqual(body['rules'][0]['match_count'], 2)
        self.assertEqual(body['rules'][0]['pattern'], 'AR|*')

    def test_listing_can_be_scoped_to_one_account(self):
        self._create(pattern='AR|*')
        self._create(pattern='BBC*', account_id=self.acct.id)
        self.assertEqual(len(self.client.get(self.BASE).get_json()['rules']), 2)
        scoped = self.client.get(f'{self.BASE}?account_id={self.acct.id}').get_json()
        self.assertEqual([r['pattern'] for r in scoped['rules']], ['BBC*'])

    def test_editing_a_missing_rule_is_a_404(self):
        self.assertEqual(self.client.patch(f'{self.BASE}/9999', json={}).status_code, 404)

    def test_deleting_a_missing_rule_is_a_404(self):
        self.assertEqual(self.client.delete(f'{self.BASE}/9999').status_code, 404)

    def test_a_trailing_space_in_a_pattern_survives(self):
        """`DE: ` and `RO| ` are real category prefixes on real accounts; the trailing space
        is load-bearing and must not be stripped on the way in."""
        self._channel('Something', category='DE: Sport')
        db.session.commit()
        body = self._create(target=HIDE_TARGET_CATEGORY_GLOB, pattern='DE: *').get_json()
        self.assertEqual(body['rule']['pattern'], 'DE: *')
        self.assertEqual(body['preview']['matched'], 1)


class RefusedSaveIsVisibleTests(_RuleTestCase):
    """A saved rule that has not been applied is invisible by construction: nothing errors,
    the channel is simply still offered. So the refusal queues a retry and says so on the
    Hide Rules page itself - principle 1, on the one path where staying quiet would be
    indistinguishable from working.

    It raised a CHANNEL_HIDE_RULES_NOT_APPLIED alert until dev/changelog/928. The surface
    moved to the page the person who just saved the rule is already looking at, derived from
    the queued retry rather than stored, so nothing can leave a stale "not applied" claim
    behind once the retry succeeds. That makes the real jobstore load-bearing here: the
    pending retry IS the state being reported.
    """

    START_SCHEDULER = True

    def tearDown(self):
        admission.reset_for_tests()
        super().tearDown()

    def test_a_refused_save_keeps_the_rule_and_says_so(self):
        self._channel('AR| One')
        self._channel('BBC')   # so 'AR|*' is not a hides-everything rule, which 409s first
        db.session.commit()
        admission.try_start(admission.KIND_SYNC, 'account 1')
        resp = self._post('/api/channel-hide-rules',
                          {'target': HIDE_TARGET_NAME_GLOB, 'pattern': 'AR|*'})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['success'])
        self.assertFalse(body['materialized'])
        self.assertIn('account sync', body['refusal'])
        # The rule is durable even though the pass did not run - that is what makes a
        # refusal harmless, and what lets the retry reach the same answer.
        self.assertEqual(ChannelHideRule.query.count(), 1)
        db.session.expire_all()
        self.assertEqual(self._hidden_names(), [])

    def test_a_refused_save_is_reported_on_the_page_naming_the_blocker(self):
        from app.scheduler import pending_hide_materialize

        self._channel('AR| One')
        self._channel('BBC')
        db.session.commit()
        admission.try_start(admission.KIND_SYNC, 'account 1')
        self._post('/api/channel-hide-rules',
                   {'target': HIDE_TARGET_NAME_GLOB, 'pattern': 'AR|*'})

        pending = pending_hide_materialize()
        self.assertIsNotNone(pending, 'a refused pass must leave a pending retry to report')
        self.assertIn('account sync', pending['reason'])
        self.assertIsNotNone(pending['retry_at'])

        page = self.t.client.get('/channels/hide-rules')
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('not applied to your channels yet', html)
        self.assertIn('account sync', html)

    def test_no_refusal_means_no_banner(self):
        self._channel('AR| One')
        self._channel('BBC')
        db.session.commit()
        self._post('/api/channel-hide-rules',
                   {'target': HIDE_TARGET_NAME_GLOB, 'pattern': 'AR|*'})

        self.assertNotIn('not applied to your channels yet',
                         self.t.client.get('/channels/hide-rules').get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
