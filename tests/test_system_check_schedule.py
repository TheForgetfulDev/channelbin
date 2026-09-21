"""Tier 2 - the automatic "TV Guide Channels" health check's schedule is the user's, and
every surface that claims its coverage tells the truth about it (dev/changelog/1068).

Guards, in order:
  * The system check unschedules through the same route as any other check. It used to be
    refused with "always scheduled - pause its schedule instead", which pointed at a
    control that only exists while the check is recurring - so a user who had switched it
    to a one-off had no way to turn it off at all.
  * Unscheduling really disarms it: status leaves SCHEDULED, the recurrence and the
    APScheduler job are gone, and check_window.due_jobs() (SCHEDULED-only) will not
    dispatch it.
  * It is a round trip, not a one-way door - /reschedule re-arms it.
  * The check itself is still undeletable. Removing the schedule is not removing the check.
  * Coverage claims read schedule_is_live(), so a group covered only by the automatic check
    is told "No coverage" rather than "Inherited coverage" once it stops running, on the
    groups list and on the group's own page.
  * A paused recurrence is named exactly once in a check chip's tooltip - _recur_label()
    already appends "(Paused)" and the chip used to append a second one.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import ChannelGroup, OnDemandTestJob  # noqa: E402
from app.channel_groups import schedule_is_live  # noqa: E402


class _Base(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        self.acc = seed.make_account()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _system_job(self):
        db.session.expire_all()
        return OnDemandTestJob.query.filter_by(is_system=True).first()

    def _covered_group(self, name='Inherited only'):
        """A stored group whose only coverage is the automatic check - it holds a guide
        channel and carries no health check of its own."""
        ch = seed.make_channel(self.acc, name=f'{name} feed', in_guide=True,
                               test_enabled=True)
        grp = seed.make_group(name=name, members=[ch], in_guide=True)
        db.session.commit()
        return grp


class SystemCheckUnscheduleTests(_Base):

    def test_the_system_check_can_be_unscheduled(self):
        job = self._system_job()
        self.assertEqual('SCHEDULED', job.status, 'precondition: seeded scheduled')
        resp = self.client.post(f'/api/channel-tests/on-demand/{job.id}/unschedule')
        self.assertEqual(200, resp.status_code, resp.get_json())

    def test_unscheduling_clears_the_recurrence_and_the_scheduler_job(self):
        job = self._system_job()
        self.client.post(f'/api/channel-tests/on-demand/{job.id}/unschedule')
        job = self._system_job()
        self.assertNotEqual('SCHEDULED', job.status)
        self.assertFalse(job.recurring)
        self.assertIsNone(job.recur_day)
        self.assertIsNone(job.scheduled_start_time)
        self.assertIsNone(job.scheduler_job_id)

    def test_an_unscheduled_system_check_is_not_dispatched_by_the_window(self):
        from datetime import datetime
        from app.check_window import due_jobs

        job_id = self._system_job().id
        # Window mode first, so the job really is one due_jobs() would otherwise dispatch -
        # the seeded exact-time schedule is not window-dispatched and would pass this
        # vacuously.
        job = self._system_job()
        job.recur_use_window = True
        db.session.commit()
        self.assertIn(job_id, [j.id for j in due_jobs(datetime.utcnow())],
                      'precondition: the window would dispatch it while scheduled')
        self.client.post(f'/api/channel-tests/on-demand/{job_id}/unschedule')
        self.assertNotIn(job_id, [j.id for j in due_jobs(datetime.utcnow())])

    def test_the_schedule_can_be_put_back(self):
        from datetime import datetime, timedelta
        from unittest import mock

        job_id = self._system_job().id
        self.client.post(f'/api/channel-tests/on-demand/{job_id}/unschedule')
        future = datetime.utcnow() + timedelta(days=1)
        with mock.patch('app.scheduler.schedule_on_demand_job',
                        return_value=('od_job_sys', future)):
            resp = self.client.post(f'/api/channel-tests/on-demand/{job_id}/reschedule',
                                    json={'recurring': True, 'recur_day': 0,
                                          'recur_time': '02:00'})
        self.assertEqual(200, resp.status_code, resp.get_json())
        job = self._system_job()
        self.assertEqual('SCHEDULED', job.status)
        self.assertTrue(job.recurring)

    def test_removing_the_schedule_is_not_removing_the_check(self):
        job_id = self._system_job().id
        self.client.post(f'/api/channel-tests/on-demand/{job_id}/unschedule')
        resp = self.client.delete(f'/api/channel-tests/on-demand/{job_id}')
        self.assertEqual(400, resp.status_code)
        self.assertIsNotNone(self._system_job())

    def test_the_system_group_page_offers_unschedule_while_scheduled(self):
        sys_group = ChannelGroup.query.filter_by(is_system=True).first()
        html = self.client.get(f'/channel-groups/{sys_group.id}').data.decode()
        self.assertIn('data-act="unschedule"', html)


class ScheduleIsLiveTests(_Base):

    def test_a_scheduled_check_is_live(self):
        self.assertTrue(schedule_is_live(self._system_job()))

    def test_a_paused_recurrence_is_not_live(self):
        job = self._system_job()
        job.recur_paused = True
        db.session.commit()
        self.assertFalse(schedule_is_live(job))

    def test_an_unscheduled_check_is_not_live(self):
        job_id = self._system_job().id
        self.client.post(f'/api/channel-tests/on-demand/{job_id}/unschedule')
        self.assertFalse(schedule_is_live(self._system_job()))


class InheritedCoverageHonestyTests(_Base):

    def _chip_label(self, html, group_name):
        """The visible label of the check chip on `group_name`'s row."""
        row = html.find(f'{group_name}</span>')
        self.assertNotEqual(row, -1, f'{group_name} row not found')
        chunk = html[row:row + 3000]
        attr = chunk.find('data-menu-check=')
        self.assertNotEqual(attr, -1, f'{group_name} has no check chip')
        close = chunk.find('>', attr)
        return chunk[close + 1:chunk.find('</span>', close)]

    def test_inherited_coverage_is_claimed_while_the_check_runs(self):
        grp = self._covered_group()
        html = self.client.get('/channel-groups').data.decode()
        self.assertEqual('Inherited coverage', self._chip_label(html, grp.name))

    def test_inherited_coverage_is_withdrawn_once_the_check_is_unscheduled(self):
        grp = self._covered_group()
        job_id = self._system_job().id
        self.client.post(f'/api/channel-tests/on-demand/{job_id}/unschedule')
        html = self.client.get('/channel-groups').data.decode()
        self.assertEqual('No coverage', self._chip_label(html, grp.name))

    def test_the_group_page_says_nothing_is_testing_it(self):
        grp = self._covered_group()
        job_id = self._system_job().id
        self.client.post(f'/api/channel-tests/on-demand/{job_id}/unschedule')
        html = self.client.get(f'/channel-groups/{grp.id}').data.decode()
        self.assertIn('Nothing is testing these channels', html)

    def test_the_group_page_claims_coverage_while_the_check_runs(self):
        grp = self._covered_group()
        html = self.client.get(f'/channel-groups/{grp.id}').data.decode()
        self.assertNotIn('Nothing is testing these channels', html)
        self.assertIn('The automatic TV Guide check tests one member on', html)

    def test_a_paused_recurrence_is_named_once_in_the_chip_tooltip(self):
        """_recur_label() already appends "(Paused)"; the chip appended a second one, so
        the tooltip read "... (Paused) (paused)"."""
        chans = [seed.make_channel(self.acc, name='Paused chip feed')]
        grp = seed.make_group(name='Paused chip group', members=chans)
        db.session.add(OnDemandTestJob(name='Nightly', group_id=grp.id,
                                       status='SCHEDULED', recurring=True, recur_day=0,
                                       recur_hour=2, recur_minute=0, recur_paused=True))
        db.session.commit()
        html = self.client.get('/channel-groups').data.decode()
        start = html.find('data-tip="Nightly.')
        self.assertNotEqual(start, -1, 'the Nightly chip has no tooltip')
        tip = html[start:html.find('"', start + len('data-tip="'))]
        self.assertEqual(1, tip.lower().count('(paused)'), tip)


if __name__ == '__main__':
    unittest.main(verbosity=2)
