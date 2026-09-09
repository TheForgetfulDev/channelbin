"""Hiding a channel: the four columns, the one recompute, and the two doors.

`Channel.hidden` is a materialized cache over three inputs - the human's own override, the
rules (not built yet), and guide/group protection. A cache maintained incrementally at ~16
call sites is only as good as its worst-maintained one, and a stale `hidden` has nothing to
notice it: the channel simply is or is not offered, with no error anywhere. So the load-
bearing test here is `IncrementalMatchesFromScratchTests`, in the shape of
`test_guide_scope_consistency.py` - it drives the real routes and asserts after every
mutation that the maintained answer equals a from-scratch recompute.

The other test that cannot be skipped is `DuplicateKeepCascadeTests`. `_duplicate_losers()`
ranks the KEPT copy of a shared-URL cluster over the whole channels table, so without a
hidden rung at the top of that cascade, hiding the copy that happened to win makes every
remaining copy a loser - and with `hidden` and `dup` both defaulting on, the stream vanishes
from the search entirely instead of falling back to a visible copy.

dev/changelog/775.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import channel_hiding  # noqa: E402
from app.channel_search import (  # noqa: E402
    SearchContext, SearchState, default_standing_for, standing_applied, GRAIN_CHANNELS)
from app.database import Channel, ChannelEvent, CHANNEL_HIDE_OVERRIDE_CHANGED  # noqa: E402


class _HidingTestCase(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        # These drive the real routes, and CSRF is app-wide - a token is exactly the part a
        # browser supplies and a test client does not. What is under test here is the
        # recompute hook behind each route, not the protection in front of it
        # (tests/test_csrf.py owns that).
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.ctx_mgr = self.t.app.app_context()
        self.ctx_mgr.push()
        self.acct = seed.make_account(name='Alpha')

    def tearDown(self):
        self.ctx_mgr.pop()
        self.t.cleanup()

    def _channel(self, name, **kw):
        return seed.make_channel(self.acct, name=name, **kw)

    def _hide(self, channel, value=True):
        channel_hiding.set_hidden_override(channel, value)
        channel_hiding.recompute([channel.id])
        db.session.commit()

    def search_names(self, standing=None):
        """The default channel-grain search, which is what every picker inherits."""
        if standing is None:
            standing = default_standing_for(GRAIN_CHANNELS)
        state = SearchState(grain=GRAIN_CHANNELS, standing=frozenset(standing))
        return sorted(r.name for r in search_rows(state))


def search_rows(state):
    from app.channel_search import search
    return search(state, SearchContext.build({})).rows


# ---------------------------------------------------------------------------


class RecomputeSemanticsTests(_HidingTestCase):
    """The four columns, and what each input does to them."""

    def test_a_hand_hide_hides_and_names_itself(self):
        ch = self._channel('Ordinary')
        self._hide(ch)
        self.assertTrue(ch.hidden)
        self.assertFalse(ch.hidden_deferred)
        self.assertEqual(ch.hidden_reason, channel_hiding.HIDE_REASON_MANUAL)

    def test_a_guide_row_defers_a_hide_rather_than_refusing_it(self):
        """Deliberate, and the one that keeps the bulk door usable: the intent is stored
        and honored the moment the blocker clears, instead of making the user remove the
        channel from the guide and come back."""
        ch = self._channel('In The Guide', in_guide=True)
        self._hide(ch)
        self.assertTrue(ch.hidden_override)
        self.assertFalse(ch.hidden)
        self.assertTrue(ch.hidden_deferred)

    def test_a_group_membership_defers_a_hide_too(self):
        ch = self._channel('Grouped')
        seed.make_group(name='G', members=[ch], in_guide=False)
        db.session.commit()
        self._hide(ch)
        self.assertFalse(ch.hidden)
        self.assertTrue(ch.hidden_deferred)

    def test_any_group_protects_not_only_an_in_guide_one(self):
        """A health-check-only group is one the user curated and is monitoring."""
        ch = self._channel('Monitored')
        seed.make_group(name='Checks', members=[ch], in_guide=False)
        db.session.commit()
        self._hide(ch)
        self.assertTrue(ch.hidden_deferred)

    def test_losing_the_last_protection_lets_a_deferred_hide_take_effect(self):
        ch = self._channel('Deferred', in_guide=True)
        self._hide(ch)
        self.assertTrue(ch.hidden_deferred)
        ch.in_guide = False
        channel_hiding.recompute([ch.id])
        db.session.commit()
        self.assertTrue(ch.hidden)
        self.assertFalse(ch.hidden_deferred)

    def test_force_show_is_not_the_same_as_no_answer(self):
        """False survives every rule written afterwards; None hands the answer back."""
        ch = self._channel('Always Shown')
        self._hide(ch, False)
        self.assertFalse(ch.hidden)
        self.assertIs(ch.hidden_override, False)
        self.assertIsNone(ch.hidden_reason)

    def test_undoing_a_hide_clears_the_reason(self):
        ch = self._channel('Undone')
        self._hide(ch)
        self._hide(ch, None)
        self.assertFalse(ch.hidden)
        self.assertIsNone(ch.hidden_override)
        self.assertIsNone(ch.hidden_reason)

    def test_recompute_with_no_ids_covers_the_whole_table(self):
        a, b = self._channel('A'), self._channel('B')
        for ch in (a, b):
            channel_hiding.set_hidden_override(ch, True)
        db.session.commit()
        # Nothing recomputed yet, so the cache still says visible.
        self.assertFalse(a.hidden)
        channel_hiding.recompute()
        db.session.commit()
        self.assertTrue(a.hidden)
        self.assertTrue(b.hidden)

    def test_an_empty_id_list_touches_nothing(self):
        ch = self._channel('Untouched')
        channel_hiding.set_hidden_override(ch, True)
        self.assertEqual(channel_hiding.recompute([]), 0)
        db.session.commit()
        self.assertFalse(ch.hidden)

    def test_recompute_sees_a_pending_change_the_caller_has_not_flushed(self):
        """A CHARACTERIZATION test, not a regression guard - it was verified to still pass
        with `recompute()`'s own `db.session.flush()` removed, because SQLAlchemy autoflushes
        an ORM-enabled UPDATE anyway today.

        The explicit flush stays regardless: whether a given statement autoflushes is a
        detail of how the statement was built, and this pass reads its inputs in SQL, so
        losing it would be silent - the recompute would succeed while answering against the
        previous state. What this test pins is the behaviour every caller relies on, which is
        that handing `recompute()` an unflushed change is legal."""
        ch = self._channel('Pending')
        ch.hidden_override = True     # hide-override-write-ok: exercising the flush, not the writer
        channel_hiding.recompute([ch.id])
        db.session.commit()
        self.assertTrue(ch.hidden)


class SetHiddenOverrideTests(_HidingTestCase):

    def test_it_logs_the_move(self):
        ch = self._channel('Logged')
        self.assertTrue(channel_hiding.set_hidden_override(ch, True))
        db.session.commit()
        events = ChannelEvent.query.filter_by(
            channel_id=ch.id, event_type=CHANNEL_HIDE_OVERRIDE_CHANGED).all()
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0].extra_data)['new'], True)

    def test_a_switch_that_did_not_move_is_not_something_that_happened(self):
        ch = self._channel('Noop')
        channel_hiding.set_hidden_override(ch, True)
        db.session.commit()
        self.assertFalse(channel_hiding.set_hidden_override(ch, True))
        db.session.commit()
        self.assertEqual(ChannelEvent.query.filter_by(
            channel_id=ch.id, event_type=CHANNEL_HIDE_OVERRIDE_CHANGED).count(), 1)

    def test_it_refuses_anything_that_is_not_the_tri_state(self):
        ch = self._channel('Strict')
        for bad in (1, 0, 'true', ''):
            with self.assertRaises(ValueError):
                channel_hiding.set_hidden_override(ch, bad)


class HideStateTests(_HidingTestCase):
    """One sentence, built in one place, so three surfaces cannot word it three ways."""

    def test_it_names_the_protection_that_is_holding_a_hide(self):
        ch = self._channel('Held', in_guide=True)
        self._hide(ch)
        st = channel_hiding.hide_state(ch, in_group=False)
        self.assertEqual(st['label'], 'Hidden. Kept visible because it is in the TV Guide.')
        self.assertEqual(st['protected_by'], 'guide')

    def test_it_names_both_when_both_apply(self):
        ch = self._channel('Both', in_guide=True)
        self._hide(ch)
        st = channel_hiding.hide_state(ch, in_group=True)
        self.assertEqual(st['protected_by'], 'both')
        self.assertIn('in the TV Guide and in a channel group', st['label'])

    def test_an_ordinary_visible_channel_says_nothing(self):
        self.assertEqual(channel_hiding.hide_state(self._channel('Quiet'))['label'], '')


class SearchExposureTests(_HidingTestCase):
    """The channel search is the only surface that can expose a hidden channel."""

    def test_a_hidden_channel_is_gone_from_the_default_search(self):
        self._channel('Visible')
        self._hide(self._channel('Gone'))
        self.assertEqual(self.search_names(), ['Visible'])

    def test_ticking_show_hidden_channels_brings_it_back(self):
        self._channel('Visible')
        self._hide(self._channel('Gone'))
        showing = set(default_standing_for(GRAIN_CHANNELS)) | {'showhidden'}
        self.assertEqual(self.search_names(standing=showing), ['Gone', 'Visible'])

    def test_hiding_applies_by_default(self):
        """Asserted as behavior, not membership: since the inversion (dev/changelog/778)
        the option hides while its key is ABSENT, so `assertIn` would now pin the opposite
        of what this test is named for."""
        self.assertTrue(standing_applied(default_standing_for(GRAIN_CHANNELS), 'showhidden'))
        self.assertNotIn('showhidden', default_standing_for(GRAIN_CHANNELS))


class DuplicateKeepCascadeTests(_HidingTestCase):
    """Hiding the KEPT copy of a duplicate cluster must promote a visible one, not make the
    whole stream disappear.

    `_duplicate_losers()` ranks over the whole channels table on purpose, so it does not know
    or care what the current search asks for. Before the hidden rung existed, hiding the
    winner left every other copy ranked below a row that `hidden` then also removed, and with
    both options defaulting on the result was zero rows for that stream.
    """

    def setUp(self):
        super().setUp()
        url = 'http://dupe.test/live/9'
        self.first = self._channel('Copy One', is_duplicate_stream_url=True)
        self.second = self._channel('Copy Two', is_duplicate_stream_url=True)
        self.first.stream_url = self.second.stream_url = url
        db.session.commit()

    def test_the_lowest_id_wins_when_nothing_is_hidden(self):
        self.assertEqual(self.search_names(), ['Copy One'])

    def test_hiding_the_winner_promotes_the_other_copy(self):
        self._hide(self.first)
        self.assertEqual(self.search_names(), ['Copy Two'])

    def test_the_python_keep_rank_agrees_with_the_sql(self):
        """`channel_search_rows._keep_rank()` explains what `_duplicate_losers()` decides.
        A disagreement is a KEPT badge naming a rule the list did not follow."""
        from app.channel_search_rows import _keep_rank
        self._hide(self.first)
        db.session.expire_all()
        rows = Channel.query.order_by(Channel.id).all()
        best = sorted(rows, key=lambda ch: _keep_rank(_KeepRow(ch)))[0]
        self.assertEqual(best.name, 'Copy Two')


class _KeepRow:
    """The shape `_keep_rank()` reads - the query it normally consumes selects these five
    values as labelled columns rather than whole Channel rows."""

    def __init__(self, ch):
        self.id = ch.id
        self.hidden = ch.hidden
        self.in_guide = ch.in_guide
        self.in_group = bool(ch.group_memberships)
        self.health = ch.health_score


class IncrementalMatchesFromScratchTests(_HidingTestCase):
    """The cache, maintained by ~16 hooks, must equal what a full recompute would say.

    Every mutation below goes through the real route or the real helper, not through
    `recompute()` directly - a hook that never fires is exactly what this is looking for, and
    calling the recompute here by hand would hide it.
    """

    def setUp(self):
        super().setUp()
        self.client = self.t.app.test_client()
        self.a = self._channel('Alpha Feed')
        self.b = self._channel('Bravo Feed')
        self.c = self._channel('Charlie Feed')
        db.session.commit()
        for ch in (self.a, self.b, self.c):
            self._hide(ch)

    def assert_consistent(self, note):
        """A full recompute must be a no-op. Compared as a snapshot rather than a rowcount:
        the UPDATE rewrites every row it touches whether or not the value changed, so a
        rowcount would pass even when the answers moved."""
        db.session.expire_all()
        before = {ch.id: (ch.hidden, ch.hidden_deferred, ch.hidden_reason)
                  for ch in Channel.query.all()}
        channel_hiding.recompute()
        db.session.commit()
        db.session.expire_all()
        after = {ch.id: (ch.hidden, ch.hidden_deferred, ch.hidden_reason)
                 for ch in Channel.query.all()}
        self.assertEqual(before, after, f'hidden went stale after {note}')

    def test_adding_and_removing_a_guide_row_through_the_route(self):
        self.client.post(f'/channels/{self.a.id}/toggle', data={'confirm': '1'})
        self.assert_consistent('adding a guide row')
        db.session.expire_all()
        self.assertTrue(db.session.get(Channel, self.a.id).hidden_deferred)

        self.client.post(f'/channels/{self.a.id}/toggle', data={'confirm': '1'})
        self.assert_consistent('removing a guide row')
        db.session.expire_all()
        self.assertTrue(db.session.get(Channel, self.a.id).hidden)

    def test_creating_a_group_through_the_route(self):
        r = self.client.post('/api/channel-groups', json={
            'name': 'Made Of Hidden Channels',
            'channel_ids': [self.a.id, self.b.id],
            'allow_format_mismatch': True, 'force': True,
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assert_consistent('creating a group')
        db.session.expire_all()
        self.assertTrue(db.session.get(Channel, self.a.id).hidden_deferred)
        self.assertTrue(db.session.get(Channel, self.c.id).hidden)

    def test_adding_and_removing_a_member_through_the_routes(self):
        group = seed.make_group(name='Existing', members=[self.a], in_guide=False)
        db.session.commit()
        gid = group.id
        channel_hiding.recompute()
        db.session.commit()

        r = self.client.post(f'/api/channel-groups/{gid}/members',
                             json={'channel_ids': [self.b.id],
                                   'allow_format_mismatch': True, 'force': True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assert_consistent('adding a member')

        r = self.client.post(f'/api/channel-groups/{gid}/members/remove',
                             json={'channel_ids': [self.b.id], 'confirm': True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assert_consistent('removing a member')
        db.session.expire_all()
        self.assertTrue(db.session.get(Channel, self.b.id).hidden)

    def test_deleting_the_group_through_the_route(self):
        group = seed.make_group(name='Doomed', members=[self.a, self.b], in_guide=False)
        db.session.commit()
        gid = group.id
        channel_hiding.recompute()
        db.session.commit()
        db.session.expire_all()
        self.assertTrue(db.session.get(Channel, self.a.id).hidden_deferred)

        r = self.client.post(f'/api/channel-groups/{gid}/delete', json={'confirm': True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assert_consistent('deleting the group')
        db.session.expire_all()
        self.assertTrue(db.session.get(Channel, self.a.id).hidden)


class DoorTests(_HidingTestCase):
    """The two ways a person hides something."""

    def setUp(self):
        super().setUp()
        self.client = self.t.app.test_client()

    def test_the_detail_page_door_hides_one_channel(self):
        ch = self._channel('One At A Time')
        r = self.client.post(f'/channels/{ch.id}/hide', json={'override': True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['hide']['hidden'])
        db.session.expire_all()
        self.assertTrue(db.session.get(Channel, ch.id).hidden)

    def test_the_detail_page_stays_reachable_by_id_while_hidden(self):
        """Going directly to a hidden channel's page must still show it - it is where
        the un-hide lives, so hiding cannot take it away."""
        ch = self._channel('Still Reachable')
        self.client.post(f'/channels/{ch.id}/hide', json={'override': True})
        self.assertEqual(self.client.get(f'/channels/{ch.id}').status_code, 200)

    def test_the_bulk_door_hides_the_rest_and_reports_the_protected_one(self):
        """100 selected with 1 in the guide hides 99 and names the 1, rather than failing
        the batch over one row."""
        free_a = self._channel('Free A')
        free_b = self._channel('Free B')
        protected = self._channel('Protected', in_guide=True)
        db.session.commit()
        ids = [free_a.id, free_b.id, protected.id]

        r = self.client.post('/api/channels/hide',
                             json={'channel_ids': ids, 'override': True})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['hidden'], 2)
        self.assertEqual([d['name'] for d in body['deferred']], ['Protected'])

    def test_the_bulk_door_un_hides(self):
        ch = self._channel('Coming Back')
        self._hide(ch)
        r = self.client.post('/api/channels/hide',
                             json={'channel_ids': [ch.id], 'override': None})
        self.assertEqual(r.status_code, 200)
        db.session.expire_all()
        self.assertFalse(db.session.get(Channel, ch.id).hidden)

    def test_a_bad_override_is_a_400_rather_than_a_guess(self):
        ch = self._channel('Strict Door')
        for body in ({'channel_ids': [ch.id]},
                     {'channel_ids': [ch.id], 'override': 'yes'},
                     {'channel_ids': [ch.id], 'override': 1}):
            r = self.client.post('/api/channels/hide', json=body)
            self.assertEqual(r.status_code, 400, body)

    def test_an_empty_selection_is_a_400(self):
        r = self.client.post('/api/channels/hide',
                             json={'channel_ids': [], 'override': True})
        self.assertEqual(r.status_code, 400)


if __name__ == '__main__':
    unittest.main()
