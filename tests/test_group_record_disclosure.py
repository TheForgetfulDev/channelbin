"""Tier 0 - the scheduling modal's channel-group disclosure (dev/changelog/904).

Scheduling a recording against a channel group does not record "the group": the recorder
picks ONE member at record-start time (format lock filters, health score ranks) and can
hand off to another mid-run. The shared record modal treated a group target exactly like a
single channel and named none of that, which is product principle 1's own failure mode -
the app making a real choice on the user's behalf and staying silent about it.

What this covers: that `GET /api/channel-groups/<id>/record-context` answers with the same
member `app/recorder.py::start_recording` would resolve (so the modal cannot promise a feed
the recorder would not open), that the format lock's zero-survivors override is disclosed
BEFORE the recording is scheduled rather than only on the artifact afterwards, that a group
with nothing recording-enabled says so instead of falling silent, and that the wiring the
modal needs is actually present in the template and in guide.js.

What it cannot cover: how the note reads on screen or how it wraps at 375px. jsdom computes
no layout and this test never opens a browser.

Runs against a throwaway temp SQLite DB - never the live one.
  python3 -m unittest tests.test_group_record_disclosure
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import ChannelGroup  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class GroupRecordContextApiTests(unittest.TestCase):
    """The endpoint's answer to 'which member would serve this right now'."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acct = seed.make_account(name='Provider A')

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _ch(self, name, score, resolution=None, fps=None, **kw):
        ch = seed.make_channel(self.acct, name=name, health_score=score, **kw)
        if resolution is not None:
            seed.make_channel_test(ch, all_null=False, status='COMPLETED',
                                   resolution=resolution, fps=fps)
        return ch

    def _get(self, group_id):
        client = self.t.app.test_client()
        resp = client.get(f'/api/channel-groups/{group_id}/record-context')
        return resp.status_code, json.loads(resp.data)

    def test_names_the_best_scoring_recording_enabled_member(self):
        low = self._ch('Feed Low', 20.0)
        high = self._ch('Feed High', 95.0)
        mid = self._ch('Feed Mid', 60.0)
        grp = seed.make_group(name='Alpha', members=[low, high, mid])
        db.session.commit()

        status, data = self._get(grp.id)
        self.assertEqual(status, 200)
        self.assertTrue(data['success'])
        self.assertEqual(data['group']['name'], 'Alpha')
        self.assertEqual(data['serving']['id'], high.id)
        self.assertEqual(data['serving']['name'], 'Feed High')
        # The account is named because a member on a different provider is the single most
        # useful thing about which feed was picked.
        self.assertEqual(data['serving']['account_name'], 'Provider A')
        self.assertEqual(data['member_count'], 3)
        self.assertEqual(data['recording_member_count'], 3)
        self.assertFalse(data['format_override'])

    def test_a_member_with_recording_off_is_never_served(self):
        """Participation is user intent, and the endpoint reads it rather than out-ranking
        it - the best-scoring channel in the group is not a candidate at all when its
        Recording switch is off (DESIGN-channel-groups-model.md 4.1)."""
        best = self._ch('Feed Best', 99.0)
        other = self._ch('Feed Other', 40.0)
        grp = seed.make_group(name='Alpha', members=[best, other], disabled=[best.id])
        db.session.commit()

        _, data = self._get(grp.id)
        self.assertEqual(data['serving']['id'], other.id)
        self.assertEqual(data['member_count'], 2)
        self.assertEqual(data['recording_member_count'], 1)

    def test_format_lock_filters_before_health_score_ranks(self):
        """The whole reason the answer cannot be 'the highest-scoring member': a locked
        group skips a better-scoring member whose measured format differs, so a modal that
        ranked on score alone would name a feed the recorder would refuse to open."""
        off_format = self._ch('Feed 720', 95.0, resolution='1280x720', fps=30.0)
        on_format = self._ch('Feed 1080', 60.0, resolution='1920x1080', fps=60.0)
        grp = seed.make_group(name='Alpha', members=[off_format, on_format])
        grp.set_locked_format('1920x1080', 60)
        grp.format_strategy = 'manual'
        db.session.commit()

        _, data = self._get(grp.id)
        self.assertEqual(data['serving']['id'], on_format.id)
        self.assertFalse(data['format_override'])
        self.assertEqual(data['locked_format'], '1920x1080 @ 60')

    def test_zero_survivors_is_disclosed_as_an_override_not_a_refusal(self):
        """15.2's override: nothing matches the lock, so the recording still runs off the
        group's format - and the modal is the only surface that can say so while choosing
        not to schedule it is still an option."""
        a = self._ch('Feed A', 80.0, resolution='1280x720', fps=30.0)
        b = self._ch('Feed B', 50.0, resolution='1280x720', fps=30.0)
        grp = seed.make_group(name='Alpha', members=[a, b])
        grp.set_locked_format('1920x1080', 60)
        grp.format_strategy = 'manual'
        db.session.commit()

        _, data = self._get(grp.id)
        self.assertTrue(data['format_override'])
        # Still names a member - the override records, it does not skip.
        self.assertEqual(data['serving']['id'], a.id)
        self.assertEqual(data['locked_format'], '1920x1080 @ 60')

    def test_untested_member_is_not_filtered_out_by_the_lock(self):
        """Unknown is not proven-different. A never-tested member stays eligible, or a
        locked group with no health check would serve nobody at all."""
        untested = self._ch('Feed Untested', 90.0)
        tested = self._ch('Feed Tested', 40.0, resolution='1920x1080', fps=60.0)
        grp = seed.make_group(name='Alpha', members=[untested, tested])
        grp.set_locked_format('1920x1080', 60)
        grp.format_strategy = 'manual'
        db.session.commit()

        _, data = self._get(grp.id)
        self.assertEqual(data['serving']['id'], untested.id)
        self.assertFalse(data['format_override'])

    def test_group_with_no_recording_enabled_member_says_so(self):
        """A group that cannot produce a file is exactly the state the user must not have
        to infer - the endpoint answers null rather than omitting the field."""
        a = self._ch('Feed A', 80.0)
        grp = seed.make_group(name='Alpha', members=[a], recording=False)
        db.session.commit()

        status, data = self._get(grp.id)
        self.assertEqual(status, 200)
        self.assertIsNone(data['serving'])
        self.assertEqual(data['recording_member_count'], 0)

    def test_a_group_nobody_records_from_serves_nobody(self):
        """Not a recording source by construction - its members are never
        recording-enabled, so the same null answer covers it with no special case."""
        a = self._ch('Feed A', 80.0)
        grp = seed.make_group(name='Checks', members=[a], recording=False)
        db.session.commit()

        _, data = self._get(grp.id)
        self.assertIsNone(data['serving'])

    def test_system_group_is_refused(self):
        grp = ChannelGroup(name='TV Guide Channels', is_system=True, in_guide=False,
                           guide_sort_order=0)
        db.session.add(grp)
        db.session.commit()

        status, data = self._get(grp.id)
        self.assertEqual(status, 400)
        self.assertIn('error', data)

    def test_missing_group_is_404(self):
        status, data = self._get(9999)
        self.assertEqual(status, 404)
        self.assertIn('error', data)


class ServingMemberHelperTests(unittest.TestCase):
    """channel_groups.serving_member() is the ONE spelling of 'format lock filters, health
    score ranks' for display surfaces, so the guide row and the endpoint cannot disagree
    about which feed a group is showing."""

    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_guide_row_and_record_context_name_the_same_member(self):
        from app.channel_groups import guide_row_targets, serving_member
        from app.routes.channel_tests import _latest_tests_by_channel, ANY_JOB

        acct = seed.make_account()
        a = seed.make_channel(acct, name='Feed A', health_score=30.0)
        b = seed.make_channel(acct, name='Feed B', health_score=85.0)
        seed.make_channel_test(a, all_null=False, status='COMPLETED',
                               resolution='1280x720', fps=30.0)
        seed.make_channel_test(b, all_null=False, status='COMPLETED',
                               resolution='1280x720', fps=30.0)
        grp = seed.make_group(name='Alpha', members=[a, b], in_guide=True)
        db.session.commit()

        latest = _latest_tests_by_channel([a.id, b.id], for_job_id=ANY_JOB)
        rows = [e for e in guide_row_targets(latest_by_channel=latest) if e[0] == 'group']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2].id,
                         serving_member(grp, latest).member.id)
        self.assertEqual(rows[0][2].id, b.id)

    def test_selection_carries_the_override_flag(self):
        """The FormatSelection rides along so a caller disclosing the override does not
        have to re-run the filter to discover it."""
        from app.channel_groups import serving_member
        from app.routes.channel_tests import _latest_tests_by_channel, ANY_JOB

        acct = seed.make_account()
        a = seed.make_channel(acct, name='Feed A', health_score=50.0)
        seed.make_channel_test(a, all_null=False, status='COMPLETED',
                               resolution='1280x720', fps=30.0)
        grp = seed.make_group(name='Alpha', members=[a])
        grp.set_locked_format('1920x1080', 60)
        grp.format_strategy = 'manual'
        db.session.commit()

        choice = serving_member(grp, _latest_tests_by_channel([a.id], for_job_id=ANY_JOB))
        self.assertTrue(choice.selection.override)
        self.assertEqual(choice.member.id, a.id)


class ModalWiringTests(unittest.TestCase):
    """The disclosure is worthless if the element it fills is not on the page. Every
    surface that includes _record_modal.html loads guide.js, so one call site covers all
    five - which is also why the search endpoint no longer carries its own copy."""

    def _read(self, *parts):
        with open(os.path.join(REPO, *parts), encoding='utf-8') as fh:
            return fh.read()

    def test_modal_template_has_the_note_element(self):
        html = self._read('templates', '_record_modal.html')
        self.assertIn('id="modal-group-note"', html)
        # Same shared component the padding note uses, not a page-local style.
        self.assertIn('class="notice notice-info"', html)

    def test_open_modal_asks_for_the_group_note(self):
        js = self._read('static', 'js', 'guide.js')
        self.assertIn('function showGroupNote', js)
        self.assertIn('showGroupNote(prog.group_id', js)
        self.assertIn('/api/channel-groups/${groupId}/record-context', js)

    def test_note_is_hidden_for_a_non_group_target(self):
        """A single-channel recording gets no group note - a modal that left the previous
        target's group named would be worse than saying nothing."""
        js = self._read('static', 'js', 'guide.js')
        start = js.index('async function showGroupNote')
        body = js[start:start + 900]
        self.assertIn("if (!groupId)", body)
        self.assertIn("note.style.display = 'none'", body)

    def test_a_stale_fetch_cannot_paint_over_a_reopened_modal(self):
        js = self._read('static', 'js', 'guide.js')
        self.assertIn('_modalOpenToken', js)
        self.assertIn('if (token !== _modalOpenToken) return;', js)

    def test_modal_body_can_shrink_so_the_footer_stays_reachable(self):
        """dev/docs/BUGS.md 2026-09-10. .modal-panel is a flex column with a max-height, so
        .modal-body needs min-height: 0 to scroll - without it the body refuses to shrink
        below its content and pushes .modal-foot out through the bottom of the panel, off
        screen, while the page behind is scroll-locked. Every modal in the app shares this
        rule, so the guard is on the rule and not on the one note that surfaced it."""
        css = self._read('static', 'css', 'style.css')
        start = css.index('.modal-body {')
        rule = css[start:css.index('}', start)]
        self.assertIn('overflow-y: auto', rule)
        self.assertIn('min-height: 0', rule)

    def test_a_form_wrapped_modal_shrinks_at_every_level(self):
        """dev/docs/BUGS.md 2026-09-10, second half. The record modal wraps its body AND its
        foot in a <form>, so the form - not .modal-body - is .modal-panel's flex item. The
        rule above is unreachable unless the form is a shrinkable flex column too: measured
        at 375px, the foot hung 6px below the panel's own 85vh box with only .modal-body
        fixed. A modal built this way is one link, and the chain breaks at the first
        non-flex one."""
        css = self._read('static', 'css', 'style.css')
        start = css.index('.modal-panel > form {')
        rule = css[start:css.index('}', start)]
        for decl in ('display: flex', 'flex-direction: column', 'min-height: 0'):
            self.assertIn(decl, rule)
        # The shape the rule exists for. If the modal stops wrapping its foot in the form,
        # this rule is no longer what keeps the buttons on screen and should be revisited.
        html = self._read('templates', '_record_modal.html')
        form_at = html.index('<form id="modal-form"')
        form_end = html.index('</form>', form_at)
        self.assertLess(html.index('class="modal-body"'), form_end)
        self.assertLess(html.index('class="modal-foot"'), form_end)

    def test_airing_record_context_no_longer_carries_a_second_group_copy(self):
        """It shipped unconsumed long enough to grow a docstring claiming the modal used
        it. The modal asks the group endpoint instead, from all five of its surfaces."""
        py = self._read('app', 'routes', 'channel_search.py')
        start = py.index('def airing_record_context_api')
        end = py.index('def channel_search_catalog_api')
        body = py[start:end]
        self.assertNotIn("'group': None if chosen_group is None", body)


if __name__ == '__main__':
    unittest.main()
