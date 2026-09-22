"""The Groups list page (/channel-groups) against the design that produced it.

DESIGN.md §14 governs this page; §14.5 is its kebab and §14.6 its toolbar and filters.
The page was the last surface still describing the pre-one-check model
(dev/changelog/1078), so each case here is one of the claims that move made:

  * A group carries exactly one health check, so the row draws exactly one chip for it
    and nothing on the page creates another. Both controls that used to
    (`+ Add health check` / `+ Add another health check`, and the kebab's
    `Schedule health check` when it was a create) posted a shape the route answers with
    a 409 (dev/changelog/1077).
  * That chip names every state it can be in. A check has six, enumerated once in
    `routes/channel_groups.py::_check_state`, and the template's last branch is an
    unknown status rather than the rendering of a real state (CLAUDE.md "states are
    enumerated").
  * The automatic TV Guide check's incidental coverage is a SECOND, different fact and
    keeps its own chip - including when it is not scheduled and therefore covers nothing
    (dev/changelog/1068).
  * The kebab offers one `Test now` with no check name in it, and `Schedule health check`
    is a link into the group's own Settings rather than a second schedule dialog. No
    trailing `...` on any item (§14.5), sentence case throughout (§6).
  * The Health check filter's buckets read the group's OWN check's schedule. Folding the
    inherited one in filed a group with no schedule of its own under "On a schedule".
  * The "Health checks attached" sort is gone: it sorted a number that is always 1.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_groups_page_conformance
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import OnDemandTestJob  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(REPO, 'templates', 'channels', 'groups.html')


def _row(html, group_name):
    """Just one group's row markup - the page renders several and a whole-page `in`
    answers about the wrong one."""
    start = html.find(f'{group_name}</span>')
    assert start != -1, f'{group_name} row not found'
    return html[start:start + 6000]


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.client
        self.acc = seed.make_account()

    def tearDown(self):
        self.t.cleanup()

    def _group(self, name='Group A', **kw):
        ch = seed.make_channel(self.acc, name=f'{name} feed')
        grp = seed.make_group(name=name, members=[ch], in_guide=False, **kw)
        db.session.commit()
        return grp

    def _html(self):
        resp = self.client.get('/channel-groups')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)


class CheckChipTests(_Base):
    """One chip for the group's one check, and it names the state it is in."""

    def _chip_label(self, html, group_name):
        chunk = _row(html, group_name)
        m = re.search(r'<span class="(?:badge b-running|grp-check-chip)[^"]*"(?![^>]*data-menu-check)'
                      r'[^>]*>(.*?)</span>\s*$', chunk[:chunk.find('<div class="grp-actions">')],
                      re.S | re.M)
        self.assertIsNotNone(m, f'{group_name} has no chip for its own check')
        return re.sub(r'<[^>]+>', '', m.group(1)).strip()

    def test_a_check_with_no_schedule_says_so(self):
        grp = self._group(name='Unscheduled')
        seed.set_check(grp, status='QUEUED')
        db.session.commit()
        self.assertEqual('No schedule', self._chip_label(self._html(), grp.name))

    def test_a_completed_one_off_also_reads_as_no_schedule(self):
        """COMPLETED is not a schedule - the run is over and nothing will fire again.
        The chip used to read "ran <date>", which describes the past where every other
        state on this row describes what happens next."""
        grp = self._group(name='Finished')
        seed.set_check(grp, status='COMPLETED')
        db.session.commit()
        self.assertEqual('No schedule', self._chip_label(self._html(), grp.name))

    def test_a_recurrence_reads_as_its_cadence(self):
        grp = self._group(name='Nightly group')
        seed.set_check(grp, status='SCHEDULED', recurring=True, recur_day=0,
                       recur_hour=3, recur_minute=0)
        db.session.commit()
        label = self._chip_label(self._html(), grp.name)
        self.assertIn('Every day at 3:00', label)

    def test_a_paused_recurrence_is_still_named_as_one(self):
        grp = self._group(name='Paused group')
        seed.set_check(grp, status='SCHEDULED', recurring=True, recur_day=0,
                       recur_hour=3, recur_minute=0, recur_paused=True)
        db.session.commit()
        label = self._chip_label(self._html(), grp.name)
        self.assertIn('Paused', label)
        # _recur_label() already appends it; the chip must not add a second
        # (dev/docs/BUGS.md 2026-09-21 @ 08:04:00 AM ET).
        self.assertEqual(1, label.lower().count('paused'), label)

    def test_a_one_off_still_ahead_names_when(self):
        from datetime import datetime, timedelta
        grp = self._group(name='One time group')
        seed.set_check(grp, status='SCHEDULED', recurring=False,
                       scheduled_start_time=datetime.utcnow() + timedelta(days=1))
        db.session.commit()
        self.assertTrue(self._chip_label(self._html(), grp.name).startswith('One time,'))

    def test_a_cancelled_check_says_cancelled_rather_than_naming_a_schedule(self):
        grp = self._group(name='Cancelled group')
        seed.set_check(grp, status='CANCELLED')
        db.session.commit()
        self.assertEqual('Cancelled', self._chip_label(self._html(), grp.name))

    def test_a_running_check_says_running(self):
        grp = self._group(name='Running group')
        seed.set_check(grp, status='RUNNING')
        db.session.commit()
        self.assertEqual('Running', self._chip_label(self._html(), grp.name))

    def test_every_state_the_route_names_has_a_branch_in_the_template(self):
        """The enumeration lives in one place; the template renders it. A state added
        there without a branch here falls into the unknown-status chip, which is loud -
        this case is what makes that a deliberate choice rather than an oversight."""
        from app.routes.channel_groups import _check_state
        import inspect
        src_fn = inspect.getsource(_check_state)
        body = src_fn[src_fn.index('"""', src_fn.index('"""') + 3):]
        states = set(re.findall(r"'([a-z]+)'", body))
        with open(TEMPLATE, encoding='utf-8') as fh:
            src = fh.read()
        rendered = set(re.findall(r"c\.state == '([a-z]+)'", src))
        self.assertEqual(states, rendered,
                         'the chip and _check_state() disagree about what states exist')

    def test_the_chip_is_not_a_link_to_the_check(self):
        """A check's own URL redirects to its group's page (dev/changelog/1077), which is
        where clicking the row already goes - so a second navigation target on the row
        does nothing but swallow the click."""
        grp = self._group(name='Linkless')
        chunk = _row(self._html(), grp.name)
        self.assertNotIn(f'data-menu-check="{grp.id}:{grp.check.id}"', chunk)


class NoCreateCheckControlTests(_Base):
    """Nothing on this page creates a health check."""

    def test_no_pill_adds_a_check(self):
        self._group(name='Pill free')
        html = self._html()
        self.assertNotIn('+ Add health check', html)
        self.assertNotIn('+ Add another health check', html)
        self.assertNotIn('data-act="create-check"', html)

    def test_the_kebab_schedules_through_the_groups_own_settings(self):
        grp = self._group(name='Kebab group')
        chunk = _row(self._html(), grp.name)
        self.assertIn(f'/channel-groups/{grp.id}?settings=check', chunk)
        self.assertIn('Schedule health check', chunk)

    def test_test_now_appears_once_and_never_names_the_check(self):
        grp = self._group(name='Verb group')
        seed.set_check(grp, name='Some Check Name')
        db.session.commit()
        chunk = _row(self._html(), grp.name)
        self.assertEqual(1, chunk.count('>Test now<'))
        self.assertNotIn('Some Check Name', chunk)
        self.assertNotIn('Run now', chunk)
        self.assertNotIn('Test again', chunk)

    def test_no_kebab_item_carries_a_trailing_ellipsis(self):
        """DESIGN.md §14.5: nearly every item opens a modal or a gate, so the ellipsis
        distinguished nothing."""
        self._group(name='Ellipsis group')
        html = self._html()
        menus = re.findall(r'<div class="menu">(.*?)</div>', html, re.S)
        self.assertTrue(menus)
        for menu in menus:
            for label in re.findall(r'>([^<>]+)</(?:button|a)>', menu):
                self.assertFalse(label.strip().endswith(('...', '…')), label)


class InheritedCoverageChipTests(_Base):
    """The automatic check's coverage is a different fact and keeps its own chip."""

    def _system_job(self):
        db.session.expire_all()
        return OnDemandTestJob.query.filter_by(is_system=True).one()

    def _covered(self):
        """A stored group holding a guide channel, so the app's own seeded automatic
        check covers the member serving it."""
        job = self._system_job()
        job.status = 'SCHEDULED'
        job.recurring = True
        job.recur_day = 0
        job.recur_hour = 2
        job.recur_minute = 0
        job.recur_paused = False
        guide_ch = seed.make_channel(self.acc, name='Covered feed', in_guide=True,
                                     test_enabled=True)
        grp = seed.make_group(name='Covered group', members=[guide_ch], in_guide=False)
        db.session.commit()
        return grp

    def test_a_live_automatic_check_claims_inherited_coverage(self):
        grp = self._covered()
        chunk = _row(self._html(), grp.name)
        self.assertIn('Inherited coverage', chunk)

    def test_an_unscheduled_automatic_check_says_it_adds_nothing(self):
        """dev/changelog/1068: a claim of coverage must never outlive the schedule behind
        it. The wording names the automatic check specifically, because beside a group
        chip reading "Every day at 3:00" a bare "No coverage" reads as a contradiction."""
        grp = self._covered()
        job = self._system_job()
        job.status = 'QUEUED'
        job.recurring = False
        db.session.commit()
        chunk = _row(self._html(), grp.name)
        self.assertIn('No inherited coverage', chunk)

    def test_the_inherited_chip_is_a_link_because_it_names_another_group(self):
        grp = self._covered()
        job = self._system_job()
        chunk = _row(self._html(), grp.name)
        self.assertIn(f'data-menu-check="{grp.id}:{job.id}"', chunk)


class FilterAndSortTests(_Base):
    """§14.6. The Health check dimension answers a question about the group's own
    schedule; the checks-attached sort answers nothing."""

    def _dim(self, html, group_name):
        chunk = html[:html.find(f'{group_name}</span>')]
        return re.findall(r'data-check="([a-z]+)"', chunk)[-1]

    def test_a_recurring_check_files_under_recur(self):
        grp = self._group(name='Recur group')
        seed.set_check(grp, status='SCHEDULED', recurring=True, recur_day=0,
                       recur_hour=3, recur_minute=0)
        db.session.commit()
        self.assertEqual('recur', self._dim(self._html(), grp.name))

    def test_a_running_recurrence_still_files_under_recur(self):
        """status carries the run state as well as the schedule state, so reading it
        alone files a recurring check as unscheduled for as long as it is running - the
        same conflation dev/changelog/1077 fixed in the monitored predicate."""
        grp = self._group(name='Busy recur group')
        seed.set_check(grp, status='RUNNING', recurring=True, recur_day=0,
                       recur_hour=3, recur_minute=0)
        db.session.commit()
        self.assertEqual('recur', self._dim(self._html(), grp.name))

    def test_a_group_with_no_schedule_of_its_own_files_under_none(self):
        """Even when the automatic check covers a member on its behalf: the filter is how
        somebody finds the holes in their own monitoring, and inherited coverage reaches
        one member, not the group."""
        guide_ch = seed.make_channel(self.acc, name='Covered feed', in_guide=True,
                                     test_enabled=True)
        grp = seed.make_group(name='Uncovered group', members=[guide_ch], in_guide=False)
        db.session.commit()
        self.assertEqual('none', self._dim(self._html(), grp.name))

    def test_the_checks_attached_sort_is_gone(self):
        self._group(name='Sort group')
        html = self._html()
        self.assertNotIn('data-gsort="checks"', html)
        self.assertNotIn('data-checks-count', html)


if __name__ == '__main__':
    unittest.main()
