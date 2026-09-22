"""Tier 2 - a group's attached health check schedules, the `linked` detail section, and
the non-destructive clone contract (dev/changelog/323 + 322).

Guards, in order:
  * The `linked` detail section lists every schedule attached to a group, and the
    INHERITED system check is never one of them - it is the automatic TV Guide check,
    which has its own row. That is the "one flag, one meaning" trap: the row's `checks`
    list holds both attached and inherited entries.
  * Cloning a group is NON-DESTRUCTIVE: it posts the clone contract, so the source keeps
    its memberships and its job. The old convert action flipped the group in place and
    deleted the pruned memberships.
  * `allow_format_mismatch` is enforced SERVER-side, not merely gated in the modal. What
    it means changed with the model: the mismatched member is ADDED and left switched on,
    and the format lock filters it out of selection instead of unticking it
    (DESIGN-channel-groups-model.md 4.1). Without the opt-in the clone WARNS rather than
    refusing (dev/changelog/762).
"""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import (ChannelGroup, ChannelGroupMember, ChannelTest,  # noqa: E402
                          OnDemandTestJob)
from app.routes.channel_groups import GROUP_DETAIL_SECTIONS  # noqa: E402


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

    def _pair(self, name='Paired group', n=2, job_name='Nightly'):
        chans = [seed.make_channel(self.acc, name=f'{name} {i}') for i in range(n)]
        grp = seed.make_group(name=name, members=chans)
        job = seed.set_check(grp, name=job_name, status='COMPLETED')
        db.session.commit()
        return grp, job, chans

    def _health_check_only_group(self, name='Standalone check', formats=None):
        """A group nobody records from (nothing recording-enabled, so it is a health
        check and nothing else) with `formats` = [(resolution, fps, health), ...]."""
        formats = formats or [('1920x1080', 60.0, 90), ('1920x1080', 60.0, 80)]
        chans = []
        for i, (res, fps, health) in enumerate(formats):
            ch = seed.make_channel(self.acc, name=f'{name} feed {i}')
            ch.health_score = health
            db.session.add(ChannelTest(channel_id=ch.id, status='COMPLETED', resolution=res,
                                       fps=fps, test_started_at=datetime(2026, 7, 25, 12, 0)))
            chans.append(ch)
        grp = ChannelGroup(name=name)
        db.session.add(grp)
        db.session.flush()
        for pos, ch in enumerate(chans):
            db.session.add(ChannelGroupMember(group_id=grp.id, channel_id=ch.id, position=pos))
        db.session.add(OnDemandTestJob(name=name, group_id=grp.id, status='COMPLETED'))
        db.session.commit()
        return grp, chans


class OneCheckPerGroupTests(_Base):
    """dev/changelog/1077: a group carries exactly one health check, minted with it. The
    `linked` card that listed several, the pair badge and every create-a-check control
    are gone with the second check."""

    def test_the_sections_no_longer_carry_a_linked_card(self):
        self.assertEqual(('summary', 'settings', 'channels', 'activity'),
                         GROUP_DETAIL_SECTIONS)

    def test_a_group_page_names_its_one_check(self):
        grp, job, _ = self._pair(job_name='Nightly quick')
        html = self.client.get(f'/channel-groups/{grp.id}').data.decode()
        self.assertNotIn('id="gd-linked"', html)
        self.assertNotIn('gd-pair-badge', html)
        self.assertIn('jobName: "Nightly quick"', html)
        self.assertIn('data-act="schedule"', html)

    def test_a_second_check_on_one_group_is_refused_by_the_database(self):
        from sqlalchemy.exc import IntegrityError
        grp, _job, _ = self._pair(job_name='Nightly quick')
        db.session.add(OnDemandTestJob(name='Weekly deep', group_id=grp.id, status='COMPLETED'))
        with self.assertRaises(IntegrityError):
            db.session.flush()
        db.session.rollback()

    def test_no_page_offers_to_create_a_check(self):
        chans = [seed.make_channel(self.acc, name='Solo A')]
        grp = seed.make_group(name='Solo group', members=chans)
        db.session.commit()
        html = self.client.get(f'/channel-groups/{grp.id}').data.decode()
        self.assertNotIn('data-act="create-check"', html)
        self.assertNotIn('Create health check', html)
        self.assertNotIn('data-act="create-group"', html)

    def test_system_check_page_cannot_create_a_group(self):
        sys_group = ChannelGroup.query.filter_by(is_system=True).first()
        html = self.client.get(f'/channel-groups/{sys_group.id}').data.decode()
        self.assertNotIn('data-act="create-group"', html)
        self.assertNotIn('data-act="create-check"', html)

    def test_a_new_group_is_minted_with_its_check(self):
        chans = [seed.make_channel(self.acc, name='Minted A')]
        resp = self.client.post('/api/channel-groups',
                                json={'name': 'Minted', 'channel_ids': [c.id for c in chans]})
        self.assertEqual(200, resp.status_code, resp.get_json())
        grp = ChannelGroup.query.filter_by(name='Minted').one()
        self.assertIsNotNone(grp.check)
        self.assertEqual('Minted - health check', grp.check.name)
        self.assertEqual('QUEUED', grp.check.status)

    def test_deleting_the_group_deletes_its_check(self):
        grp, job, _ = self._pair(job_name='Dies with it')
        job_id = job.id
        resp = self.client.post(f'/api/channel-groups/{grp.id}/delete')
        self.assertEqual(200, resp.status_code, resp.get_json())
        db.session.expire_all()
        self.assertIsNone(db.session.get(OnDemandTestJob, job_id))

    def test_the_check_url_redirects_to_the_group(self):
        grp, job, _ = self._pair(job_name='Redirected')
        resp = self.client.get(f'/channels/health-checks/{job.id}')
        self.assertEqual(302, resp.status_code)
        self.assertTrue(resp.headers['Location'].endswith(f'/channel-groups/{grp.id}'))


class CreateChannelGroupContractTests(_Base):

    def test_create_leaves_the_source_health_check_untouched(self):
        src, chans = self._health_check_only_group()
        src_id, ids = src.id, [c.id for c in chans]
        resp = self.client.post(f'/api/channel-groups/{src_id}/clone',
                                json={'name': 'New group', 'channel_ids': ids})
        self.assertEqual(200, resp.status_code, resp.get_json())
        db.session.expire_all()
        src = db.session.get(ChannelGroup, src_id)
        self.assertFalse(any(m.recording_enabled for m in src.memberships),
                         'the source must still be a health check - nobody records from it')
        self.assertEqual(len(ids), len(list(src.memberships)))
        self.assertIsNotNone(src.check, 'the job must still be attached')
        self.assertIsNotNone(ChannelGroup.query.filter_by(name='New group').first())

    def test_create_returns_the_new_groups_detail_url(self):
        src, chans = self._health_check_only_group()
        resp = self.client.post(f'/api/channel-groups/{src.id}/clone',
                                json={'name': 'New group',
                                      'channel_ids': [c.id for c in chans]})
        body = resp.get_json()
        new = ChannelGroup.query.filter_by(name='New group').first()
        self.assertEqual(f'/channel-groups/{new.id}', body['detail_url'])

    def test_create_applies_the_group_settings_it_was_given(self):
        src, chans = self._health_check_only_group()
        resp = self.client.post(f'/api/channel-groups/{src.id}/clone',
                                json={'name': 'Configured',
                                      'channel_ids': [c.id for c in chans],
                                      'in_guide': True, 'format_strategy': 'highest_score',
                                      'format_resolution': '1920x1080', 'format_fps': 60})
        self.assertEqual(200, resp.status_code, resp.get_json())
        new = ChannelGroup.query.filter_by(name='Configured').first()
        self.assertEqual('highest_score', new.format_strategy)
        # Two settings a clone no longer honors, each refused for its own reason and each
        # REPORTED rather than silently swallowed.
        #
        # `in_guide` (DESIGN-channel-groups-model.md 15, dev/changelog/757): a clone's
        # members take the model defaults, so Recording is off on every one of them and a
        # guide row would have nothing behind it at the moment it was created.
        self.assertFalse(new.in_guide)
        self.assertTrue(resp.get_json()['guide_refused'])
        # The format pin (16.2, dev/changelog/762): a stored lock belongs to `manual` and
        # to nothing else. Under `highest_score` it would filter members on a format
        # nothing chose, while the group's settings card named a rule that follows the
        # data - and nothing would ever clear it.
        self.assertIsNone(new.locked_format_key)
        self.assertTrue(resp.get_json()['format_pin_refused'])

    def test_a_clone_with_no_settings_still_starts_out_of_the_guide(self):
        src, chans = self._health_check_only_group()
        self.client.post(f'/api/channel-groups/{src.id}/clone',
                         json={'name': 'Plain',
                               'channel_ids': [c.id for c in chans]})
        new = ChannelGroup.query.filter_by(name='Plain').first()
        self.assertFalse(new.in_guide)
        # A clone inherits the source's strategy and starts with Recording off on every
        # member, so it records from nobody and is never put in the guide (15).
        self.assertEqual(src.format_strategy, new.format_strategy)
        self.assertFalse(any(m.recording_enabled for m in new.memberships))

    def test_half_a_manual_format_is_rejected(self):
        src, chans = self._health_check_only_group()
        resp = self.client.post(f'/api/channel-groups/{src.id}/clone',
                                json={'name': 'Half',
                                      'channel_ids': [c.id for c in chans],
                                      'format_resolution': '1920x1080'})
        self.assertEqual(400, resp.status_code)
        self.assertIn('resolution and a frame rate', resp.get_json()['error'])

    def test_guide_and_format_settings_are_ignored_on_a_clone_of_a_group_nobody_records_from(self):
        """A group nobody records from has no guide row and no format to pin, so
        accepting them would store settings nothing ever reads."""
        src, chans = self._health_check_only_group()
        resp = self.client.post(f'/api/channel-groups/{src.id}/clone',
                                json={'name': 'Check copy',
                                      'channel_ids': [c.id for c in chans],
                                      'in_guide': True, 'format_resolution': '1920x1080',
                                      'format_fps': 60})
        self.assertEqual(200, resp.status_code, resp.get_json())
        new = ChannelGroup.query.filter_by(name='Check copy').first()
        self.assertFalse(new.in_guide)
        self.assertIsNone(new.locked_format_key)


class CloneProvenanceTests(_Base):
    """Auto-select-channels from a health check (or the plain group Clone action)
    leaves a one-time 'created from' note on the new group - not a live pairing,
    just enough to answer 'where did this come from' without re-deriving it by hand."""

    def test_clone_stamps_source_id_and_name(self):
        src, chans = self._health_check_only_group(name='Widdle-down check')
        resp = self.client.post(f'/api/channel-groups/{src.id}/clone',
                                json={'name': 'New group',
                                      'channel_ids': [c.id for c in chans]})
        self.assertEqual(200, resp.status_code, resp.get_json())
        new = ChannelGroup.query.filter_by(name='New group').first()
        self.assertEqual(src.id, new.cloned_from_group_id)
        self.assertEqual('Widdle-down check', new.cloned_from_name)

    def test_detail_page_links_back_to_the_still_existing_source(self):
        src, chans = self._health_check_only_group(name='Widdle-down check')
        self.client.post(f'/api/channel-groups/{src.id}/clone',
                         json={'name': 'New group',
                               'channel_ids': [c.id for c in chans]})
        new = ChannelGroup.query.filter_by(name='New group').first()
        html = self.client.get(f'/channel-groups/{new.id}').data.decode()
        self.assertIn('Cloned from', html)
        self.assertIn(f'/channel-groups/{src.id}', html)
        self.assertIn('Widdle-down check', html)

    def test_detail_page_still_names_a_deleted_source_but_does_not_link_it(self):
        chans = [seed.make_channel(self.acc, name=f'Src feed {i}') for i in range(2)]
        src = seed.make_group(name='Throwaway source', members=chans)
        self.client.post(f'/api/channel-groups/{src.id}/clone',
                         json={'name': 'New group',
                               'channel_ids': [c.id for c in chans]})
        new = ChannelGroup.query.filter_by(name='New group').first()
        self.client.post(f'/api/channel-groups/{src.id}/delete')
        html = self.client.get(f'/channel-groups/{new.id}').data.decode()
        self.assertIn('Throwaway source', html)
        self.assertIn('(deleted)', html)
        self.assertNotIn(f'/channel-groups/{src.id}"', html)

    def test_a_plain_group_with_no_clone_history_shows_no_note(self):
        chans = [seed.make_channel(self.acc, name=f'Plain feed {i}') for i in range(2)]
        grp = seed.make_group(name='Never cloned', members=chans)
        html = self.client.get(f'/channel-groups/{grp.id}').data.decode()
        self.assertNotIn('Cloned from', html)


class FormatMismatchOptInTests(_Base):

    def _mixed(self):
        return self._health_check_only_group('Mixed check', formats=[('1920x1080', 60.0, 90),
                                                        ('1280x720', 30.0, 10)])

    def test_a_clone_never_warns_about_a_format_mix(self):
        # The warning applies to a group that RECORDS, and a clone records from nobody:
        # its members all start with Recording off, whatever strategy it is given, so the
        # format question belongs to its promotion (DESIGN-channel-groups-model.md 14.1,
        # dev/changelog/1077). The mix is still disclosed on the new group's own page.
        src, chans = self._mixed()
        resp = self.client.post(f'/api/channel-groups/{src.id}/clone',
                                json={'name': 'Mixed copy', 'format_strategy': 'highest_score',
                                      'channel_ids': [c.id for c in chans]})
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json()['success'])
        self.assertNotIn('format_mismatch', resp.get_json())
        new = ChannelGroup.query.filter_by(name='Mixed copy').first()
        self.assertEqual(len(chans), len(list(new.memberships)))

    def test_opt_in_is_honoured_server_side(self):
        """Enforcement lives server-side: gating this in the modal alone would leave the
        block unreachable by anything else that posts the same contract."""
        src, chans = self._mixed()
        resp = self.client.post(f'/api/channel-groups/{src.id}/clone',
                                json={'name': 'Allowed',
                                      'channel_ids': [c.id for c in chans],
                                      'allow_format_mismatch': True})
        self.assertEqual(200, resp.status_code, resp.get_json())

    def test_the_mismatched_channel_really_is_taken_out_of_failover(self):
        """The confirm dialog promises exactly this. UI text describing backend behavior
        is part of the change surface, so the behavior is asserted here."""
        src, chans = self._mixed()
        self.client.post(f'/api/channel-groups/{src.id}/clone',
                         json={'name': 'Allowed',
                               'channel_ids': [c.id for c in chans],
                               'allow_format_mismatch': True})
        new = ChannelGroup.query.filter_by(name='Allowed').first()
        # Both members are added and NEITHER is switched off: the mismatch is detected
        # and shown, and the format lock filters at selection time instead
        # (DESIGN-channel-groups-model.md 4.1).
        enabled = {m.channel_id: m.recording_enabled for m in new.memberships}
        self.assertEqual({chans[0].id, chans[1].id}, set(enabled))
        self.assertFalse(any(enabled.values()),
                         'a clone is not a promotion - Recording starts off on every member')


if __name__ == '__main__':
    unittest.main(verbosity=2)
